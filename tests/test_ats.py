import json
from pathlib import Path

import httpx

from agent.config import EXAMPLE_DIR, load_companies
from agent.discover import ashby, greenhouse, lever
from agent.discover.ats import discover_ats

from tests.fakes import NOW

FIXTURES = Path(__file__).parent / "fixtures"
COMPANY = {"name": "Acme Corp", "platform": "greenhouse", "token": "acme", "tier": "A"}


def load(name):
    return json.loads((FIXTURES / name).read_text())


def test_greenhouse_parse():
    jobs = greenhouse.parse(load("greenhouse.json"), COMPANY)
    first, second = jobs
    assert (first.source, first.external_id, first.company, first.company_tier) == ("greenhouse", "7001", "Acme Corp", "A")
    assert first.description == "Build Java services with Kafka & PostgreSQL."
    assert first.posted_at == "2026-09-30T10:00:00-04:00"
    assert not first.remote
    assert second.remote
    assert second.posted_at == "2026-10-02T10:00:00-04:00"


def test_lever_parse():
    first, second = lever.parse(load("lever.json"), COMPANY)
    assert (first.source, first.external_id, first.title, first.location) == ("lever", "a1b2c3", "Backend Engineer II", "Bangalore")
    assert "3+ years of Java experience" in first.description
    assert "Requirements" in first.description
    assert first.salary_text == "INR 2500000 - 3500000 per-year-salary"
    assert first.posted_at.startswith("2026-")
    assert first.apply_url.endswith("/apply")
    assert (second.location, second.remote, second.salary_text) == ("Remote", True, "")


def test_ashby_parse_skips_unlisted():
    jobs = ashby.parse(load("ashby.json"), COMPANY)
    assert len(jobs) == 1
    job = jobs[0]
    assert (job.source, job.external_id, job.location, job.remote) == ("ashby", "f1e2d3c4", "Remote / London", True)
    assert job.salary_text == "$150K - $190K"
    assert job.description == "Python and Go."


def client_for(routes):
    def handler(request):
        for host, payload in routes.items():
            if host in request.url.host:
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discover_ats_survives_a_failing_company(conn):
    companies = [
        {"name": "Acme", "platform": "greenhouse", "token": "acme", "tier": "A"},
        {"name": "Gone", "platform": "lever", "token": "gone", "tier": "B"},
        {"name": "Acme", "platform": "ashby", "token": "acme", "tier": "A"},
    ]
    client = client_for({"greenhouse.io": load("greenhouse.json"), "ashbyhq.com": load("ashby.json")})
    stats, errors = discover_ats(conn, client, companies, NOW)
    assert stats == {"fetched": 3, "new": 3}
    assert len(errors) == 1 and errors[0].startswith("lever:gone: HTTPStatusError")
    again, _ = discover_ats(conn, client, companies, NOW)
    assert again == {"fetched": 3, "new": 0}
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_discover_ats_handles_malformed_payload(conn):
    client = client_for({"greenhouse.io": {"jobs": [{"title": "no id"}]}})
    stats, errors = discover_ats(conn, client, [COMPANY], NOW)
    assert stats == {"fetched": 0, "new": 0}
    assert errors[0].startswith("greenhouse:acme: KeyError")


def test_companies_seed_is_valid():
    companies = load_companies(EXAMPLE_DIR)
    assert len(companies) == 148
    assert {c["platform"] for c in companies} == {"greenhouse", "lever", "ashby"}
    assert all(c["tier"] in {"A", "B"} and c["name"] for c in companies)
    keys = [(c["platform"], c["token"]) for c in companies]
    assert len(keys) == len(set(keys))
