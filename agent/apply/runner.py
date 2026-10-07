import sqlite3
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import httpx
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from agent.db import update_job

TERMINAL = ("applied", "needs_you", "failed")
RETRYABLE = (httpx.TransportError, PlaywrightTimeoutError)
MAX_ERRORS = 3


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, RETRYABLE) or (isinstance(exc, PlaywrightError) and "net::ERR_" in str(exc))


def _errors(conn: sqlite3.Connection, job_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM applications WHERE job_id = ? AND status = 'error'", (job_id,)).fetchone()[0]


def _record(conn: sqlite3.Connection, job_id: int, ts: str, mode: str, status: str, reason: str, app_dir: Path) -> None:
    conn.execute(
        "INSERT INTO applications (job_id, ts, mode, status, reason, dir) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, ts, mode, status, reason, str(app_dir)),
    )
    conn.commit()


def _applied_today(conn: sqlite3.Connection, day: str) -> Counter:
    rows = conn.execute(
        "SELECT j.company FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.mode = 'live' AND a.status = 'applied' AND a.ts LIKE ?",
        (f"{day}%",),
    ).fetchall()
    return Counter(row[0] for row in rows)


def _applied_ever(conn: sqlite3.Connection) -> Counter:
    rows = conn.execute(
        "SELECT j.company FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.mode = 'live' AND a.status = 'applied'"
    ).fetchall()
    return Counter(row[0] for row in rows)


def run_applications(conn: sqlite3.Connection, settings: dict, drivers: dict[str, Callable], apps_dir: Path, now: Callable[[], str],
                     sleep: Callable[[float], None], dry_run: bool, stop_file: Path, limit: int | None = None) -> dict:
    stats: Counter = Counter()
    mode = "dry_run" if dry_run else "live"
    per_company = _applied_today(conn, now()[:10])
    total = sum(per_company.values())
    ever = _applied_ever(conn)
    total_cap = settings.get("per_company_total_cap")
    exempt_markets = set(settings.get("total_cap_exempt_markets") or [])
    sources = tuple(drivers)
    jobs = conn.execute(
        f"SELECT * FROM jobs WHERE status = 'ready' AND source IN ({','.join('?' * len(sources))}) ORDER BY score DESC, id",
        sources,
    ).fetchall()
    failures = 0
    attempted = 0
    halted = ""
    for job in jobs:
        if limit is not None and attempted >= limit:
            break
        if not dry_run and total >= settings["daily_cap"]:
            halted = "daily cap"
            break
        if not dry_run and per_company[job["company"]] >= settings["per_company_daily_cap"]:
            continue
        if not dry_run and total_cap is not None and job["market"] not in exempt_markets and ever[job["company"]] >= total_cap:
            continue
        if stop_file.exists():
            halted = "STOP file"
            break
        if attempted and not dry_run:
            sleep(settings["min_seconds_between_submissions"])
        attempted += 1
        app_dir = apps_dir / str(job["id"])
        app_dir.mkdir(parents=True, exist_ok=True)
        try:
            status, reason = drivers[job["source"]](job, app_dir, not dry_run)
        except Exception as exc:
            status = "error" if _retryable(exc) else "failed"
            reason = f"{exc.__class__.__name__}: {str(exc)[:200]}"
        stats[status] += 1
        _record(conn, job["id"], now(), mode, status, reason, app_dir)
        if not dry_run and status == "error" and _errors(conn, job["id"]) >= MAX_ERRORS:
            update_job(conn, job["id"], status="failed", reason=reason)
        elif not dry_run and status in TERMINAL:
            update_job(conn, job["id"], status=status, reason=reason)
        if status == "applied":
            per_company[job["company"]] += 1
            ever[job["company"]] += 1
            total += 1
        failures = failures + 1 if status in ("failed", "error") else 0
        if failures >= settings.get("max_consecutive_failures", 3):
            halted = f"{failures} consecutive failures"
            break
    result = dict(stats)
    if halted:
        result["halted"] = halted
    return result
