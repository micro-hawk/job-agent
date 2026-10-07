from datetime import datetime

import pytest

from agent.config import EXAMPLE_DIR, load_settings
from agent.db import update_job, upsert_job
from agent.filter import evaluate, min_years_required, run_filters, sponsorship_signal, title_ok
from agent.models import Job

from tests.fakes import NOW

SETTINGS = load_settings(EXAMPLE_DIR)
NOW_DT = datetime(2026, 10, 5, 9, 30)


def row(**overrides):
    base = {
        "id": 1, "source": "greenhouse", "company": "Acme", "company_tier": "A", "title": "Senior Software Engineer",
        "location": "Bengaluru, India", "remote": 0, "description": "Java, Kafka, PostgreSQL.", "salary_text": "",
        "posted_at": "2026-10-01T10:00:00+00:00",
    }
    base.update(overrides)
    return base


ALLOWED = [
    "Software Engineer", "Software Engineer I", "Software Engineer II", "Software Engineer 2", "Software Engineer II, Organizations (Auth0)",
    "Software Engineer, Pricing", "Software Engineer - India", "SDE", "SDE 2", "SDE-2", "SDE II", "Software Development Engineer II",
    "Product Engineer", "Product Engineer 2", "Product Engineer III", "Senior Software Engineer", "Sr. Software Engineer",
    "Senior Software Engineer, Datastores (Auth0)", "Senior Software Engineer - Fullstack", "[London] Senior Software Engineer",
    "Senior Backend Engineer", "Senior Backend Engineer - Data Integrations", "Senior Java Developer", "Software Engineer III", "Senior Software Engineer II",
    "Java Developer", "Backend Developer", "Senior Java Backend Developer", "Java Back End Developer", "Java Full Stack Developer",
    "Software Developer", "Senior Software Developer", "Senior Software Engineer II (Backend)", "Java Backend Developer - 3 To 20 years",
    "Software Development Engineer III", "SDE 3", "SDE III", "SDE-3", "Software Engineer II Backend", "SDE II Payments",
    "Backend Software Engineer", "Backend Systems Engineer", "Product Software Engineer", "Product Software Engineer II - (1071)",
    "Product Software Engineer - 1108", "Full Stack Java Developer", "Full - Stack Developer - Java", "Fullstack Java Engineer",
]
DENIED = [
    "Staff Software Engineer", "Staff Software Development Engineer - Java/Go", "Principal Engineer", "Engineering Manager",
    "Applied AI Engineer, Enterprise", "AI Platform Engineer", "Member of Technical Staff", "Senior Product Engineer",
    "Java Technical Lead", "Senior Java Engineering Lead, Credit Exposure- Vice President", "Senior Software Engineer - Tech Lead", "Backend Engineering Manager",
    "Senior Backend Product Engineer", "SDET II", "Senior Software Engineer in Test", "Senior Software Engineer - Infrastructure",
    "Software Engineer, Security", "Platform Engineer - Kubernetes", "Backend Engineering Intern", "Frontend Engineer", "Software Engineer Intern",
    "Full Stack Developer", "Fullstack Engineer", "MERN Full Stack Engineer", "Full - Stack Engineer - WEB", "Software Engineer II Infrastructure",
]


@pytest.mark.parametrize("title", ALLOWED)
def test_titles_allowed(title):
    assert title_ok(title, SETTINGS["titles"]) == (True, "")


@pytest.mark.parametrize("title", DENIED)
def test_titles_denied(title):
    assert not title_ok(title, SETTINGS["titles"])[0]


def test_untargeted_title():
    assert title_ok("Product Designer", SETTINGS["titles"]) == (False, "title not targeted")


@pytest.mark.parametrize(
    ("text", "signal"),
    [
        ("We are unable to sponsor visas for this role.", "negative"),
        ("We do not offer visa sponsorship.", "negative"),
        ("No visa sponsorship is available.", "negative"),
        ("You must already have the right to work in the UK.", "negative"),
        ("Sponsorship is not available for this position.", "negative"),
        ("Visa sponsorship is available for the right candidate.", "positive"),
        ("We offer a relocation package.", "positive"),
        ("We build payments.", "unknown"),
    ],
)
def test_sponsorship_signal(text, signal):
    assert sponsorship_signal(text) == signal


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("5+ years of professional software engineering experience", 5),
        ("2-5 years experience", 2),
        ("8+ years of experience; 2+ years experience with Kafka", 2),
        ("We were founded 10 years ago", None),
    ],
)
def test_min_years_required(text, years):
    assert min_years_required(text) == years


def test_evaluate_happy_path_india_tier_a():
    result = evaluate(row(), SETTINGS, NOW_DT)
    assert result.ok
    assert result.market == "india"
    assert result.salary.text == "₹28 LPA"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"title": "Product Designer"}, "title not targeted"),
        ({"posted_at": "2026-06-01T00:00:00Z"}, "posted more than 90 days ago"),
        ({"location": "Remote - US", "remote": 1}, "US location"),
        ({"location": "London, UK", "description": "We are unable to sponsor visas."}, "no visa sponsorship"),
        ({"description": "8+ years of backend experience required."}, "requires 8+ years"),
        ({"salary_text": "₹15-20 LPA"}, "range tops at ₹20 LPA"),
    ],
)
def test_evaluate_rejections(overrides, reason):
    result = evaluate(row(**overrides), SETTINGS, NOW_DT)
    assert not result.ok
    assert result.reason.startswith(reason)


def test_india_role_ignores_sponsorship_language():
    assert evaluate(row(description="No visa sponsorship."), SETTINGS, NOW_DT).ok


def test_description_single_amount_is_not_salary():
    result = evaluate(row(company_tier="B", description="We raised $50M. Java and Kafka."), SETTINGS, NOW_DT)
    assert result.ok
    assert result.salary.text == "₹25 LPA"


def test_unparseable_posted_at_is_kept():
    assert evaluate(row(posted_at="yesterday"), SETTINGS, NOW_DT).ok


def insert(conn, **fields):
    job_id, _ = upsert_job(conn, Job(**fields), NOW)
    return job_id


def test_run_filters_prefers_ats_over_alert_duplicates(conn):
    alert = insert(conn, source="linkedin", external_id="li1", company="Acme Technologies", title="Sr. Software Engineer", location="Bengaluru, India")
    ats = insert(conn, source="greenhouse", external_id="gh1", company="Acme", title="Senior Software Engineer", location="Bengaluru, India", description="Java", posted_at="2026-10-01")
    other = insert(conn, source="linkedin", external_id="li2", company="Beta", title="Software Engineer", location="Karnataka, India")
    counts = run_filters(conn, SETTINGS, NOW_DT)
    statuses = {r["id"]: (r["status"], r["reason"]) for r in conn.execute("SELECT id, status, reason FROM jobs")}
    assert statuses[ats] == ("candidate", "")
    assert statuses[alert] == ("filtered_out", f"duplicate of #{ats}")
    assert statuses[other] == ("manual", "")
    assert counts == {"candidate": 1, "manual": 1, "filtered_out": 1}


def test_run_filters_only_touches_discovered(conn):
    job_id = insert(conn, source="greenhouse", external_id="gh1", company="Acme", title="Senior Software Engineer", location="Bengaluru, India")
    update_job(conn, job_id, status="ready", score=90)
    assert run_filters(conn, SETTINGS, NOW_DT) == {"candidate": 0, "manual": 0, "filtered_out": 0}
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "ready"


def test_run_filters_stores_market_and_salary(conn):
    job_id = insert(conn, source="naukri", external_id="n1", company="Gamma", title="Software Engineer", location="Remote", salary_text="20-30 Lacs P.A.")
    run_filters(conn, SETTINGS, NOW_DT)
    stored = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert (stored["status"], stored["market"], stored["expected_salary"]) == ("manual", "india", "₹28 LPA")


def test_canada_dollar_range_is_read_as_cad():
    result = evaluate(row(location="Toronto, ON", description="Base salary $136,000 - $187,000."), {**SETTINGS, "markets": None}, NOW_DT)
    assert result.salary.currency == "CAD"
    assert result.salary.amount == 187_000


@pytest.mark.parametrize("location", ["London, UK", "Berlin, Germany", "Toronto, Canada", "Dubai, UAE"])
def test_evaluate_skips_markets_not_enabled(location):
    result = evaluate(row(location=location), SETTINGS, NOW_DT)
    assert not result.ok
    assert result.reason.startswith("market not targeted")


def test_evaluate_keeps_global_remote():
    result = evaluate(row(location="Remote - Anywhere", remote=1), SETTINGS, NOW_DT)
    assert result.market == "global_remote"
