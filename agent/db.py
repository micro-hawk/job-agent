import json
import sqlite3
from pathlib import Path

from agent.models import Job
from agent.text import jd_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    company TEXT NOT NULL,
    company_tier TEXT NOT NULL DEFAULT 'B',
    title TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    remote INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL DEFAULT '',
    apply_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    salary_text TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL DEFAULT '',
    jd_hash TEXT NOT NULL,
    dedupe_key TEXT NOT NULL DEFAULT '',
    market TEXT,
    sponsorship TEXT,
    expected_salary TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',
    reason TEXT NOT NULL DEFAULT '',
    prescore INTEGER,
    score INTEGER,
    score_detail TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key);
CREATE TABLE IF NOT EXISTS jd_summaries (
    jd_hash TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats TEXT NOT NULL DEFAULT '{}',
    errors TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    dir TEXT
);
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL UNIQUE,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    note TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS questionnaires (
    id INTEGER PRIMARY KEY,
    questionnaire_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    job_title TEXT NOT NULL,
    url TEXT NOT NULL,
    questions TEXT NOT NULL DEFAULT '[]',
    answers TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    UNIQUE (questionnaire_id, opportunity_id)
);
CREATE TABLE IF NOT EXISTS seen_emails (
    message_id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
"""

_UPDATABLE = {"status", "reason", "dedupe_key", "market", "sponsorship", "expected_salary", "prescore", "score", "score_detail"}


def connect(path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_job(conn: sqlite3.Connection, job: Job, now: str) -> tuple[int, bool]:
    row = conn.execute("SELECT id FROM jobs WHERE source=? AND external_id=?", (job.source, job.external_id)).fetchone()
    if row:
        conn.execute("UPDATE jobs SET last_seen=? WHERE id=?", (now, row["id"]))
        conn.commit()
        return row["id"], False
    cursor = conn.execute(
        """INSERT INTO jobs (source, external_id, company, company_tier, title, location, remote, url, apply_url,
                             description, salary_text, posted_at, jd_hash, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job.source, job.external_id, job.company, job.company_tier, job.title, job.location, int(job.remote),
            job.url, job.apply_url, job.description, job.salary_text, job.posted_at,
            jd_hash(job.description or f"{job.company}|{job.title}"), now, now,
        ),
    )
    conn.commit()
    return cursor.lastrowid, True


def update_job(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"unknown job columns: {sorted(unknown)}")
    assignments = ", ".join(f"{name}=?" for name in fields)
    conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))
    conn.commit()


def jobs_with_status(conn: sqlite3.Connection, *statuses: str, order: str = "id") -> list[sqlite3.Row]:
    marks = ",".join("?" * len(statuses))
    return conn.execute(f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY {order}", statuses).fetchall()


def start_run(conn: sqlite3.Connection, now: str) -> int:
    cursor = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now,))
    conn.commit()
    return cursor.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, now: str, stats: dict, errors: list[str]) -> None:
    conn.execute("UPDATE runs SET finished_at=?, stats=?, errors=? WHERE id=?", (now, json.dumps(stats), json.dumps(errors), run_id))
    conn.commit()


def is_email_seen(conn: sqlite3.Connection, message_id: str) -> bool:
    return conn.execute("SELECT 1 FROM seen_emails WHERE message_id=?", (message_id,)).fetchone() is not None


def mark_email_seen(conn: sqlite3.Connection, message_id: str, sender: str, now: str) -> None:
    conn.execute("INSERT OR IGNORE INTO seen_emails (message_id, sender, seen_at) VALUES (?, ?, ?)", (message_id, sender, now))
    conn.commit()
