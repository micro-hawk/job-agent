import httpx
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


from agent.apply.runner import run_applications
from agent.db import update_job, upsert_job
from agent.models import Job

SETTINGS = {"daily_cap": 30, "per_company_daily_cap": 3, "min_seconds_between_submissions": 20, "max_consecutive_failures": 3}
NOW = "2026-10-05T10:00:00"


def add(conn, external_id, company, score, source="greenhouse", status="ready"):
    job_id, _ = upsert_job(conn, Job(source, external_id, company, f"Engineer {external_id}"), NOW)
    update_job(conn, job_id, status=status, score=score)
    return job_id


class Driver:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, job, app_dir, submit):
        self.calls.append((job["id"], submit))
        return self.outcomes.pop(0) if self.outcomes else ("applied", "")


def run(conn, driver, tmp_path, dry_run=False, settings=SETTINGS, stop=None):
    sleeps = []
    stats = run_applications(conn, settings, {"greenhouse": driver}, tmp_path, lambda: NOW, sleeps.append, dry_run, stop or (tmp_path / "STOP"))
    return stats, sleeps


def statuses(conn):
    return dict(conn.execute("SELECT external_id, status FROM jobs").fetchall())


def test_applies_highest_score_first_with_gap(conn, tmp_path):
    add(conn, "1", "A", 70)
    add(conn, "2", "B", 90)
    add(conn, "3", "C", 80, status="below_threshold")
    driver = Driver()
    stats, sleeps = run(conn, driver, tmp_path)
    assert [c[0] for c in driver.calls] == [2, 1]
    assert all(submit for _, submit in driver.calls)
    assert statuses(conn) == {"1": "applied", "2": "applied", "3": "below_threshold"}
    assert sleeps == [20]
    assert stats["applied"] == 2
    assert conn.execute("SELECT COUNT(*) FROM applications WHERE mode='live'").fetchone()[0] == 2
    assert (tmp_path / "2").is_dir()


def test_dry_run_keeps_jobs_ready(conn, tmp_path):
    add(conn, "1", "A", 70)
    driver = Driver(("filled", ""))
    stats, sleeps = run(conn, driver, tmp_path, dry_run=True)
    assert driver.calls == [(1, False)]
    assert statuses(conn) == {"1": "ready"}
    assert stats["filled"] == 1
    assert tuple(conn.execute("SELECT mode, status FROM applications").fetchone()) == ("dry_run", "filled")


def test_caps_per_company_and_daily(conn, tmp_path):
    for i in range(5):
        add(conn, str(i), "Same", 90 - i)
    add(conn, "9", "Other", 50)
    stats, _ = run(conn, Driver(), tmp_path)
    assert stats["applied"] == 4
    assert statuses(conn)["3"] == "ready"
    stats, _ = run(conn, Driver(), tmp_path, settings={**SETTINGS, "daily_cap": 4})
    assert stats.get("applied", 0) == 0


def test_needs_you_and_unsupported_sources(conn, tmp_path):
    add(conn, "1", "A", 90)
    add(conn, "2", "B", 80, source="lever")
    stats, _ = run(conn, Driver(("needs_you", "captcha challenge shown")), tmp_path)
    row = conn.execute("SELECT status, reason FROM jobs WHERE external_id='1'").fetchone()
    assert tuple(row) == ("needs_you", "captcha challenge shown")
    assert statuses(conn)["2"] == "ready"


def test_halts_after_consecutive_failures(conn, tmp_path):
    for i in range(5):
        add(conn, str(i), f"C{i}", 90 - i)
    driver = Driver(("failed", "x"), ("failed", "y"), ("failed", "z"))
    stats, _ = run(conn, driver, tmp_path)
    assert len(driver.calls) == 3
    assert stats["halted"] == "3 consecutive failures"


def test_driver_exception_counts_as_failure(conn, tmp_path):
    add(conn, "1", "A", 90)

    def boom(job, app_dir, submit):
        raise RuntimeError("browser crashed")

    stats, _ = run(conn, boom, tmp_path)
    assert tuple(conn.execute("SELECT status, reason FROM jobs").fetchone()) == ("failed", "RuntimeError: browser crashed")


def test_stop_file_halts_before_next(conn, tmp_path):
    add(conn, "1", "A", 90)
    add(conn, "2", "B", 80)
    stop = tmp_path / "STOP"

    def driver(job, app_dir, submit):
        stop.write_text("")
        return ("applied", "")

    stats, _ = run(conn, driver, tmp_path, stop=stop)
    assert stats["applied"] == 1
    assert stats["halted"] == "STOP file"


def test_per_company_total_cap_counts_past_days(conn, tmp_path):
    old = add(conn, "0", "A", 90, status="applied")
    conn.execute("INSERT INTO applications (job_id, ts, mode, status, reason, dir) VALUES (?, '2026-10-01T10:00:00', 'live', 'applied', '', '')", (old,))
    add(conn, "1", "A", 80)
    add(conn, "2", "A", 70)
    add(conn, "3", "B", 60)
    driver = Driver()
    run(conn, driver, tmp_path, settings=SETTINGS | {"per_company_total_cap": 2})
    assert statuses(conn) == {"0": "applied", "1": "applied", "2": "ready", "3": "applied"}


def test_total_cap_skips_exempt_markets(conn, tmp_path):
    for external_id in ("0", "1"):
        old = add(conn, external_id, "A", 90, status="applied")
        conn.execute("INSERT INTO applications (job_id, ts, mode, status, reason, dir) VALUES (?, '2026-10-01T10:00:00', 'live', 'applied', '', '')", (old,))
    india = add(conn, "2", "A", 80)
    conn.execute("UPDATE jobs SET market='india' WHERE id=?", (india,))
    add(conn, "3", "A", 70)
    run(conn, Driver(), tmp_path, settings=SETTINGS | {"per_company_total_cap": 2, "total_cap_exempt_markets": ["india"]})
    assert statuses(conn)["2"] == "applied" and statuses(conn)["3"] == "ready"


def test_network_error_keeps_job_ready_for_retry(conn, tmp_path):
    add(conn, "1", "A", 90)

    def offline(job, app_dir, submit):
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")

    stats, _ = run(conn, offline, tmp_path)
    assert statuses(conn) == {"1": "ready"}
    assert stats["error"] == 1
    assert tuple(conn.execute("SELECT status, reason FROM applications").fetchone()) == ("error", "ConnectError: [Errno 8] nodename nor servname provided, or not known")


def test_page_timeout_keeps_job_ready_for_retry(conn, tmp_path):
    add(conn, "1", "A", 90)

    def slow(job, app_dir, submit):
        raise PlaywrightTimeoutError("Page.goto: Timeout 60000ms exceeded.")

    run(conn, slow, tmp_path)
    assert statuses(conn) == {"1": "ready"}


def test_job_fails_after_repeated_network_errors(conn, tmp_path):
    add(conn, "1", "A", 90)

    def offline(job, app_dir, submit):
        raise httpx.ReadTimeout("The read operation timed out")

    for _ in range(2):
        run(conn, offline, tmp_path)
    assert statuses(conn) == {"1": "ready"}
    run(conn, offline, tmp_path)
    assert tuple(conn.execute("SELECT status, reason FROM jobs").fetchone()) == ("failed", "ReadTimeout: The read operation timed out")


def test_network_errors_count_toward_halting(conn, tmp_path):
    for i in range(5):
        add(conn, str(i), f"C{i}", 90 - i)

    def offline(job, app_dir, submit):
        raise httpx.ConnectError("offline")

    stats, _ = run(conn, offline, tmp_path)
    assert stats["error"] == 3
    assert stats["halted"] == "3 consecutive failures"
