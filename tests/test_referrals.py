from agent.db import update_job, upsert_job
from agent.models import Job
from agent.referrals import build_referrals, referral_targets
from tests.fakes import NOW, StubLLM


def add(conn, external_id, company, status, source="greenhouse", market="india", title="Senior Software Engineer"):
    job_id, _ = upsert_job(conn, Job(source, external_id, company, title, location="Bengaluru", url=f"https://example.com/{external_id}", description="Skills: Java, Kafka"), NOW)
    update_job(conn, job_id, status=status, market=market)
    return job_id


def test_targets_are_india_jobs_one_per_company_preferring_applied(conn):
    add(conn, "1", "Acme", "ready")
    applied = add(conn, "2", "Acme", "applied")
    insta = add(conn, "3", "Beta", "applied", source="instahyre", market=None)
    add(conn, "4", "Gamma", "applied", market="eu")
    add(conn, "5", "Delta", "filtered_out")
    targets = referral_targets(conn)
    assert sorted((t["company"], t["id"]) for t in targets) == [("Acme", applied), ("Beta", insta)]


def test_build_referrals_drafts_once_per_company(conn):
    add(conn, "1", "Acme", "applied")
    llm = StubLLM({"referral": lambda prompt: {"note": "Hi <Name>, short note", "message": "Hi <Name>, longer message"}})
    assert build_referrals(conn, llm, "sonnet", "Candidate brief", NOW) == 1
    assert build_referrals(conn, llm, "sonnet", "Candidate brief", NOW) == 0
    assert len(llm.calls) == 1
    assert "Acme" in llm.calls[0][2] and "Senior Software Engineer" in llm.calls[0][2]
    row = conn.execute("SELECT company, note, message, status FROM referrals").fetchone()
    assert tuple(row) == ("Acme", "Hi <Name>, short note", "Hi <Name>, longer message", "todo")


def test_build_referrals_trims_long_note(conn):
    add(conn, "1", "Acme", "applied")
    llm = StubLLM({"referral": lambda prompt: {"note": "x" * 400, "message": "m"}})
    build_referrals(conn, llm, "sonnet", "brief", NOW)
    assert len(conn.execute("SELECT note FROM referrals").fetchone()[0]) <= 280
