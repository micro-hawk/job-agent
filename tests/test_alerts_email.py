import imaplib
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from agent.config import EXAMPLE_DIR, load_settings
from agent.discover.alerts_email import classify_sender, imap_date, ingest_alerts, naukri_prepare, parse_linkedin, parse_naukri
from agent.llm import LLMError

from tests.fakes import NOW, StubLLM

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = load_settings(EXAMPLE_DIR)
LINKEDIN_TEXT = (FIXTURES / "linkedin_alert.txt").read_text()
NAUKRI_HTML = (FIXTURES / "naukri_alert.html").read_text()
NAUKRI_JOBS = {
    "jobs": [
        {"job_id": "011026503388", "title": "Java API Developer", "company": "Birlasoft", "location": "Bengaluru", "salary": ""},
        {"job_id": "011026501079", "title": "Senior Software Engineer", "company": "Mastercard", "location": "Pune", "salary": "20-30 Lacs P.A."},
        {"job_id": "", "title": "", "company": "Ad", "location": "", "salary": ""},
    ]
}


def test_imap_date_is_locale_independent():
    assert imap_date(date(2026, 9, 28)) == "28-Sep-2026"


def test_parse_linkedin_alert():
    jobs = parse_linkedin(LINKEDIN_TEXT)
    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("Software Engineer II, ITC", "Nike", "Karnataka, India"),
        ("Senior Software Engineer", "Kobie", "Bengaluru"),
        ("Backend Engineer", "Acme Labs", "India (Remote)"),
    ]
    assert jobs[0].source == "linkedin"
    assert jobs[0].external_id == "4474334011"
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/4474334011/"
    assert jobs[2].remote


def test_parse_linkedin_handles_crlf_line_endings():
    jobs = parse_linkedin(LINKEDIN_TEXT.replace("\n", "\r\n"))
    assert [(j.title, j.company) for j in jobs] == [
        ("Software Engineer II, ITC", "Nike"),
        ("Senior Software Engineer", "Kobie"),
        ("Backend Engineer", "Acme Labs"),
    ]


def test_parse_linkedin_skips_digest_header():
    text = (
        "Your job alert for software developer in Pune District\n"
        "New jobs match your preferences.\n"
        "Manage alerts: https://www.linkedin.com/comm/jobs/alerts?x=1\n"
        "\n"
        "Senior Java Developer\n"
        "Barclays\n"
        "Pune City\n"
        "\n"
        "2 connections\n"
        "View job: https://www.linkedin.com/comm/jobs/view/4470000001/?t=1\n"
        "\n"
        "---------------------------------------------------------\n"
        "See all jobs on LinkedIn:  https://www.linkedin.com/comm/jobs/search-results/?k=1\n"
        "New jobs from your other alerts\n"
        '<strong class="font-bold">Senior Software Engineer</strong> jobs in Pune District\n'
        "Senior Agentic AI Engineer\n"
        "Assent\n"
        "Pune City\n"
        "View job: https://www.linkedin.com/comm/jobs/view/4470000002/?t=1\n"
    )
    jobs = parse_linkedin(text)
    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("Senior Java Developer", "Barclays", "Pune City"),
        ("Senior Agentic AI Engineer", "Assent", "Pune City"),
    ]


def test_naukri_prepare_marks_jobs_and_strips_tracking():
    text, urls = naukri_prepare(NAUKRI_HTML)
    assert "[JOB 011026503388] Java API Developer" in text
    assert "<" not in text
    assert urls == {
        "011026503388": "https://www.naukri.com/jd/job-listings-java-api-developer-birlasoft-india-limited-bengaluru-2-to-5-years-011026503388",
        "011026501079": "https://www.naukri.com/jd/job-listings-senior-software-engineer-mastercard-pune-4-to-12-years-011026501079",
    }


def test_parse_naukri_uses_haiku_for_fields_only():
    llm = StubLLM({"alert_parse": lambda prompt: NAUKRI_JOBS})
    jobs = parse_naukri(NAUKRI_HTML, llm, "haiku")
    assert len(jobs) == 2
    first, second = jobs
    assert (first.source, first.external_id, first.company) == ("naukri", "011026503388", "Birlasoft")
    assert first.url.endswith("-011026503388")
    assert first.description == "2-5 years experience"
    assert second.salary_text == "20-30 Lacs P.A."
    assert llm.calls[0][1] == "haiku"
    assert "[JOB 011026501079]" in llm.calls[0][2]


def test_parse_naukri_without_marker_uses_stable_hash():
    llm = StubLLM({"alert_parse": lambda prompt: {"jobs": [{"job_id": "", "title": "Java Developer", "company": "Recruiter Co", "location": "Remote", "salary": ""}]}})
    first = parse_naukri("<p>Java Developer at Recruiter Co</p>", llm, "haiku")[0]
    second = parse_naukri("<p>Java Developer at Recruiter Co</p>", llm, "haiku")[0]
    assert first.external_id == second.external_id
    assert len(first.external_id) == 16
    assert first.url == ""


def test_classify_sender():
    gmail = SETTINGS["gmail"]
    assert classify_sender("LinkedIn <jobalerts-noreply@linkedin.com>", gmail) == "linkedin"
    assert classify_sender("Naukri <naukrialerts@naukri.com>", gmail) == "naukri"
    assert classify_sender("Recruiter <abc123xyz@naukri.com>", gmail) == "naukri"
    assert classify_sender("Naukri <info@naukri.com>", gmail) == "skip"
    assert classify_sender("Friend <a@example.com>", gmail) == "skip"


def message(sender, message_id, plain=None, html=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Message-ID"] = message_id
    msg["Subject"] = "alert"
    if plain:
        msg.set_content(plain)
        if html:
            msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(html, subtype="html")
    return msg


def fake_fetch(messages):
    def fetch(user, password, senders, since, is_seen):
        assert user == "you@example.com"
        assert senders == ["jobalerts-noreply@linkedin.com", "naukri.com"]
        return [(mid, msg) for mid, msg in messages if not is_seen(mid)]

    return fetch


def test_ingest_alerts_marks_seen_and_retries_failed_naukri(conn):
    messages = [
        ("<li-1>", message("jobalerts-noreply@linkedin.com", "<li-1>", plain=LINKEDIN_TEXT)),
        ("<nk-1>", message("naukrialerts@naukri.com", "<nk-1>", html=NAUKRI_HTML)),
        ("<info-1>", message("info@naukri.com", "<info-1>", html="<p>Upgrade to premium</p>")),
    ]
    failing = StubLLM({"alert_parse": lambda prompt: LLMError("alert_parse/haiku: timeout")})
    stats, errors = ingest_alerts(conn, failing, SETTINGS, "pw", date(2026, 10, 5), NOW, fetch=fake_fetch(messages))
    assert stats == {"emails": 1, "jobs": 3, "new": 3}
    assert errors == ["naukri email <nk-1>: alert_parse/haiku: timeout"]

    working = StubLLM({"alert_parse": lambda prompt: NAUKRI_JOBS})
    stats, errors = ingest_alerts(conn, working, SETTINGS, "pw", date(2026, 10, 5), NOW, fetch=fake_fetch(messages))
    assert stats == {"emails": 1, "jobs": 2, "new": 2}
    assert errors == []
    assert len(working.calls) == 1

    stats, _ = ingest_alerts(conn, working, SETTINGS, "pw", date(2026, 10, 5), NOW, fetch=fake_fetch(messages))
    assert stats == {"emails": 0, "jobs": 0, "new": 0}
    assert len(working.calls) == 1


def test_ingest_alerts_reports_gmail_failure_without_raising(conn):
    def broken(*args):
        raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    stats, errors = ingest_alerts(conn, StubLLM({}), SETTINGS, "pw", date(2026, 10, 5), NOW, fetch=broken)
    assert stats == {"emails": 0, "jobs": 0, "new": 0}
    assert errors == ["gmail: error: [AUTHENTICATIONFAILED] Invalid credentials"]
    assert "pw" not in errors[0]
