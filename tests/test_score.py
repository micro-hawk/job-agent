import json
import re

from agent.config import EXAMPLE_DIR, load_settings
from agent.db import update_job, upsert_job
from agent.llm import BudgetExceeded, LLMError
from agent.models import Job
from agent.score import run_scoring

from tests.fakes import NOW, StubLLM

SETTINGS = load_settings(EXAMPLE_DIR)
BRIEF = "Senior Backend Engineer, 4 years. Java, Kafka."
SUMMARY = {"stack": ["Java", "Kafka"], "must_haves": ["Java"], "min_years": 4, "seniority": "senior", "sponsorship": "unknown", "salary": "", "location": "Bengaluru", "red_flags": []}


def candidate(conn, external_id, description="Java and Kafka", market="india"):
    job_id, _ = upsert_job(conn, Job(source="greenhouse", external_id=external_id, company="Acme", title="Backend Engineer", description=description), NOW)
    update_job(conn, job_id, status="candidate", market=market)
    return job_id


def prescores(mapping):
    def handler(prompt):
        ids = [int(value) for value in re.findall(r"^id: (\d+)$", prompt, re.M)]
        return {"scores": [{"id": job_id, "score": mapping.get(job_id, 80)} for job_id in ids]}

    return handler


def status(conn, job_id):
    return conn.execute("SELECT status, prescore, score, score_detail, reason FROM jobs WHERE id=?", (job_id,)).fetchone()


def test_full_flow_routes_models_and_sets_statuses(conn):
    strong = candidate(conn, "1", "Java, Kafka, PostgreSQL")
    weak = candidate(conn, "2", "React Native")
    llm = StubLLM({
        "extract": lambda prompt: SUMMARY,
        "prescore": prescores({weak: 40}),
        "score": lambda prompt: {"score": 85, "reasons": ["Kafka match"], "gaps": ["No Go"]},
    })
    stats = run_scoring(conn, llm, SETTINGS, BRIEF, NOW)
    assert status(conn, strong)["status"] == "ready"
    assert status(conn, strong)["score"] == 85
    assert json.loads(status(conn, strong)["score_detail"])["reasons"] == ["Kafka match"]
    assert (status(conn, weak)["status"], status(conn, weak)["reason"]) == ("low_prescore", "prescore 40")
    assert {(task, model) for task, model, _ in llm.calls} == {("extract", "haiku"), ("prescore", "haiku"), ("score", "sonnet")}
    assert stats["ready"] == 1 and stats["low_prescore"] == 1 and stats["budget_hit"] is False
    assert all(BRIEF in prompt for task, _, prompt in llm.calls if task != "extract")


def test_extraction_is_cached_by_description_hash(conn):
    candidate(conn, "1", "Same JD")
    candidate(conn, "2", "Same JD")
    llm = StubLLM({"extract": lambda prompt: SUMMARY, "prescore": prescores({}), "score": lambda prompt: {"score": 75, "reasons": [], "gaps": []}})
    run_scoring(conn, llm, SETTINGS, BRIEF, NOW)
    assert [task for task, _, _ in llm.calls].count("extract") == 1


def test_llm_error_leaves_job_for_next_run(conn):
    broken = candidate(conn, "1", "Broken JD")
    fine = candidate(conn, "2", "Fine JD")
    llm = StubLLM({
        "extract": lambda prompt: LLMError("extract/haiku: timeout") if "Broken" in prompt else SUMMARY,
        "prescore": prescores({}),
        "score": lambda prompt: {"score": 60, "reasons": [], "gaps": ["Go"]},
    })
    stats = run_scoring(conn, llm, SETTINGS, BRIEF, NOW)
    assert status(conn, broken)["status"] == "candidate"
    assert (status(conn, fine)["status"], status(conn, fine)["reason"]) == ("below_threshold", "score 60")
    assert stats["errors"] == 1


def test_failed_prescore_batch_keeps_candidates(conn):
    job_id = candidate(conn, "1")
    llm = StubLLM({"extract": lambda prompt: SUMMARY, "prescore": lambda prompt: LLMError("prescore/haiku: non-JSON output")})
    stats = run_scoring(conn, llm, SETTINGS, BRIEF, NOW)
    assert status(conn, job_id)["status"] == "candidate"
    assert stats["errors"] == 1


def test_budget_stops_gracefully(conn):
    job_id = candidate(conn, "1")
    llm = StubLLM({"extract": lambda prompt: BudgetExceeded("daily LLM budget $3.00 reached")})
    stats = run_scoring(conn, llm, SETTINGS, BRIEF, NOW)
    assert stats["budget_hit"] is True
    assert status(conn, job_id)["status"] == "candidate"


def test_rerun_makes_no_llm_calls(conn):
    candidate(conn, "1")
    handlers = {"extract": lambda prompt: SUMMARY, "prescore": prescores({}), "score": lambda prompt: {"score": 90, "reasons": [], "gaps": []}}
    run_scoring(conn, StubLLM(handlers), SETTINGS, BRIEF, NOW)
    second = StubLLM(handlers)
    run_scoring(conn, second, SETTINGS, BRIEF, NOW)
    assert second.calls == []


def test_post_extract_checks(conn):
    senior = candidate(conn, "1", "Staff-level JD")
    abroad = candidate(conn, "2", "UK JD", market="uk")
    llm = StubLLM({"extract": lambda prompt: dict(SUMMARY, min_years=9) if "Staff-level" in prompt else dict(SUMMARY, sponsorship="not_offered")})
    run_scoring(conn, llm, SETTINGS, BRIEF, NOW)
    assert (status(conn, senior)["status"], status(conn, senior)["reason"]) == ("filtered_out", "requires 9+ years")
    assert (status(conn, abroad)["status"], status(conn, abroad)["reason"]) == ("filtered_out", "no visa sponsorship")


def test_malformed_llm_payloads_count_as_errors(conn):
    job_id = candidate(conn, "1")
    llm = StubLLM({
        "extract": lambda prompt: SUMMARY,
        "prescore": lambda prompt: {"scores": [{"id": "x"}]},
    })
    stats = run_scoring(conn, llm, SETTINGS, BRIEF, NOW)
    assert status(conn, job_id)["status"] == "candidate"
    assert stats["errors"] == 1


def test_score_payload_without_score_keeps_job_prescored(conn):
    job_id = candidate(conn, "1")
    llm = StubLLM({"extract": lambda prompt: SUMMARY, "prescore": prescores({}), "score": lambda prompt: {"reasons": []}})
    stats = run_scoring(conn, llm, SETTINGS, BRIEF, NOW)
    assert status(conn, job_id)["status"] == "prescored"
    assert stats["errors"] == 1
