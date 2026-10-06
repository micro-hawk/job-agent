import json

import pytest

from agent.db import finish_run, is_email_seen, jobs_with_status, mark_email_seen, start_run, update_job, upsert_job
from agent.models import Job
from agent.text import jd_hash

from tests.fakes import NOW


def make_job(**overrides):
    fields = {"source": "greenhouse", "external_id": "1", "company": "Acme", "title": "Backend Engineer", "description": "Java and Kafka"}
    fields.update(overrides)
    return Job(**fields)


def test_upsert_inserts_then_only_touches_last_seen(conn):
    job_id, is_new = upsert_job(conn, make_job(), NOW)
    assert is_new
    update_job(conn, job_id, status="candidate")
    again_id, again_new = upsert_job(conn, make_job(description="changed"), "2026-10-06T09:30:00")
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert (again_id, again_new) == (job_id, False)
    assert row["status"] == "candidate"
    assert row["description"] == "Java and Kafka"
    assert row["first_seen"] == NOW
    assert row["last_seen"] == "2026-10-06T09:30:00"
    assert row["jd_hash"] == jd_hash("Java and Kafka")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_jd_hash_falls_back_to_company_and_title(conn):
    job_id, _ = upsert_job(conn, make_job(source="linkedin", description=""), NOW)
    row = conn.execute("SELECT jd_hash FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["jd_hash"] == jd_hash("Acme|Backend Engineer")


def test_update_job_rejects_unknown_columns(conn):
    job_id, _ = upsert_job(conn, make_job(), NOW)
    with pytest.raises(ValueError):
        update_job(conn, job_id, title="hacked")


def test_jobs_with_status_filters_and_orders(conn):
    first, _ = upsert_job(conn, make_job(external_id="1"), NOW)
    second, _ = upsert_job(conn, make_job(external_id="2"), NOW)
    update_job(conn, first, status="prescored", prescore=60)
    update_job(conn, second, status="prescored", prescore=90)
    rows = jobs_with_status(conn, "prescored", order="prescore DESC, id")
    assert [row["id"] for row in rows] == [second, first]
    assert jobs_with_status(conn, "ready") == []


def test_runs_and_seen_emails(conn):
    run_id = start_run(conn, NOW)
    finish_run(conn, run_id, "2026-10-05T09:35:00", {"ats": {"new": 3}}, ["lever:gone: HTTPStatusError"])
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert json.loads(row["stats"]) == {"ats": {"new": 3}}
    assert json.loads(row["errors"]) == ["lever:gone: HTTPStatusError"]
    assert not is_email_seen(conn, "<a@b>")
    mark_email_seen(conn, "<a@b>", "linkedin", NOW)
    mark_email_seen(conn, "<a@b>", "linkedin", NOW)
    assert is_email_seen(conn, "<a@b>")
