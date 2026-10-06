import json
import sqlite3
from collections.abc import Mapping

from agent.db import jobs_with_status, update_job
from agent.filter import ABROAD_MARKETS
from agent.llm import BudgetExceeded, LLMError

MAX_JD_CHARS = 12000
EXTRACT_SYSTEM = (
    "You condense job postings for a job-matching tool. Return only facts stated in the posting. "
    "Keep each list to at most 8 short items. Use null or 'unknown' when the posting does not say."
)
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "stack": {"type": "array", "items": {"type": "string"}},
        "must_haves": {"type": "array", "items": {"type": "string"}},
        "min_years": {"type": ["integer", "null"]},
        "seniority": {"type": "string"},
        "sponsorship": {"type": "string", "enum": ["offered", "not_offered", "unknown"]},
        "salary": {"type": "string"},
        "location": {"type": "string"},
        "red_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["stack", "must_haves", "min_years", "seniority", "sponsorship", "salary", "location", "red_flags"],
}
PRESCORE_SYSTEM = (
    "You rate how well one candidate fits several jobs, from 0 to 100. 80 or more: strong stack and seniority fit. "
    "55 to 79: plausible. Below 55: poor stack fit or wrong seniority. Return one score per job id."
)
PRESCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {"type": "object", "properties": {"id": {"type": "integer"}, "score": {"type": "integer"}}, "required": ["id", "score"]},
        }
    },
    "required": ["scores"],
}
SCORE_SYSTEM = (
    "You are a senior technical recruiter. Score the candidate's fit for the job from 0 to 100 using only the facts given. "
    "Weigh core stack overlap, seniority match for 4 years of experience, domain, and location or sponsorship constraints. "
    "Give at most 3 short reasons and at most 3 short gaps."
)
SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "reasons", "gaps"],
}


def extract(conn: sqlite3.Connection, llm, model: str, job: Mapping, now: str) -> dict:
    cached = conn.execute("SELECT summary FROM jd_summaries WHERE jd_hash=?", (job["jd_hash"],)).fetchone()
    if cached:
        return json.loads(cached["summary"])
    prompt = f"Title: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n\n{job['description'][:MAX_JD_CHARS]}"
    summary = llm.call("extract", model, EXTRACT_SYSTEM, prompt, EXTRACT_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO jd_summaries (jd_hash, summary, created_at) VALUES (?, ?, ?)", (job["jd_hash"], json.dumps(summary), now))
    conn.commit()
    return summary


def _job_block(job: Mapping, summary: dict) -> str:
    return (
        f"id: {job['id']}\ntitle: {job['title']}\ncompany: {job['company']}\nmarket: {job['market']}\n"
        f"summary: {json.dumps(summary, separators=(',', ':'))}"
    )


def _post_extract_reason(job: Mapping, summary: dict, settings: dict) -> str:
    years = summary.get("min_years")
    if isinstance(years, int) and years > settings["max_required_years"]:
        return f"requires {years}+ years"
    if job["market"] in ABROAD_MARKETS and summary.get("sponsorship") == "not_offered":
        return "no visa sponsorship"
    return ""


def run_scoring(conn: sqlite3.Connection, llm, settings: dict, brief: str, now: str) -> dict:
    stats = {"extracted": 0, "filtered_out": 0, "low_prescore": 0, "prescored": 0, "ready": 0, "below_threshold": 0, "errors": 0, "budget_hit": False}
    models = settings["models"]
    try:
        pending = []
        for job in jobs_with_status(conn, "candidate"):
            try:
                summary = extract(conn, llm, models["extract"], job, now)
            except BudgetExceeded:
                raise
            except LLMError:
                stats["errors"] += 1
                continue
            stats["extracted"] += 1
            reason = _post_extract_reason(job, summary, settings)
            if reason:
                update_job(conn, job["id"], status="filtered_out", reason=reason)
                stats["filtered_out"] += 1
                continue
            pending.append((job, summary))

        size = settings["prescore_batch_size"]
        for start in range(0, len(pending), size):
            batch = pending[start: start + size]
            prompt = f"Candidate:\n{brief}\n\nJobs:\n\n" + "\n\n".join(_job_block(job, summary) for job, summary in batch)
            try:
                data = llm.call("prescore", models["prescore"], PRESCORE_SYSTEM, prompt, PRESCORE_SCHEMA)
            except BudgetExceeded:
                raise
            except LLMError:
                stats["errors"] += 1
                continue
            try:
                scores = {int(item["id"]): int(item["score"]) for item in data.get("scores", [])}
            except (KeyError, TypeError, ValueError):
                stats["errors"] += 1
                continue
            for job, _ in batch:
                if job["id"] not in scores:
                    continue
                value = scores[job["id"]]
                new_status = "prescored" if value >= settings["prescore_threshold"] else "low_prescore"
                update_job(conn, job["id"], status=new_status, prescore=value, reason="" if new_status == "prescored" else f"prescore {value}")
                stats[new_status] += 1

        for job in jobs_with_status(conn, "prescored", order="prescore DESC, id")[: settings["score_limit_per_run"]]:
            try:
                summary = extract(conn, llm, models["extract"], job, now)
                data = llm.call("score", models["score"], SCORE_SYSTEM, f"Candidate:\n{brief}\n\nJob:\n{_job_block(job, summary)}", SCORE_SCHEMA)
            except BudgetExceeded:
                raise
            except LLMError:
                stats["errors"] += 1
                continue
            try:
                value = int(data["score"])
            except (KeyError, TypeError, ValueError):
                stats["errors"] += 1
                continue
            new_status = "ready" if value >= settings["fit_threshold"] else "below_threshold"
            update_job(conn, job["id"], status=new_status, score=value, score_detail=json.dumps(data), reason="" if new_status == "ready" else f"score {value}")
            stats[new_status] += 1
    except BudgetExceeded:
        stats["budget_hit"] = True
    return stats
