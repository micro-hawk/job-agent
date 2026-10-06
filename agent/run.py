import argparse
import json
import os
import time
from datetime import datetime

import httpx

from agent.config import DATA_DIR, STOP_FILE, load_companies, load_env, load_settings
from agent.db import connect, finish_run, start_run
from agent.discover.alerts_email import ingest_alerts
from agent.discover.ats import FETCH_ERRORS, discover_ats, fetch_company
from agent.filter import run_filters
from agent.llm import LLM
from agent.profile import load_profile, load_resume, profile_brief
from agent.score import run_scoring

DB_PATH = DATA_DIR / "agent.db"
USER_AGENT = "job-agent/0.1"


def _stamp(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def _client(settings: dict) -> httpx.Client:
    return httpx.Client(timeout=settings["http_timeout_seconds"], headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def run_pipeline(conn, settings, companies, brief, llm, client, password, now: datetime, skip_email=False, skip_score=False) -> tuple[dict, list[str]]:
    stats: dict = {}
    errors: list[str] = []
    stats["ats"], ats_errors = discover_ats(conn, client, companies, _stamp(now))
    errors += ats_errors
    if not skip_email:
        if password:
            stats["alerts"], alert_errors = ingest_alerts(conn, llm, settings, password, now.date(), _stamp(now))
            errors += alert_errors
        else:
            errors.append("gmail: GMAIL_APP_PASSWORD not set; alert emails skipped")
    stats["filter"] = run_filters(conn, settings, now)
    if not skip_score:
        stats["score"] = run_scoring(conn, llm, settings, brief, _stamp(now))
    return stats, errors


def check_companies(companies: list[dict], settings: dict) -> int:
    failures = 0
    with _client(settings) as client:
        for company in companies:
            label = f"{company['platform']:<10} {company['token']:<32}"
            try:
                print(f"ok   {label} {len(fetch_company(client, company))} jobs")
            except FETCH_ERRORS as exc:
                failures += 1
                print(f"FAIL {label} {exc.__class__.__name__}: {str(exc)[:80]}")
    print(f"{len(companies) - failures}/{len(companies)} companies reachable")
    return 1 if failures else 0


def apply_command(settings: dict, companies: list[dict], dry_run: bool, limit: int | None) -> int:
    from playwright.sync_api import sync_playwright

    from agent.apply.greenhouse import GreenhouseDriver
    from agent.apply.runner import run_applications
    from agent.apply.security_code import code_fetcher

    if not dry_run and not settings["live"]:
        print("live is false in config/settings.yaml; use --dry-run or set live: true")
        return 1
    password = os.environ.get("GMAIL_APP_PASSWORD") or ""
    conn = connect(DB_PATH)
    llm = LLM(conn, settings["daily_budget_usd"])
    tokens = {c["name"]: c["token"] for c in companies if c["platform"] == "greenhouse"}
    with _client(settings) as client, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        driver = GreenhouseDriver(
            client, browser, llm, load_profile(), load_resume(), tokens,
            code_fetcher(settings["gmail"]["user"], password) if password else (lambda started: None), datetime.now().date(),
        )
        try:
            stats = run_applications(
                conn, settings, {"greenhouse": driver}, DATA_DIR / "apps", lambda: _stamp(datetime.now()),
                time.sleep, dry_run, STOP_FILE, limit,
            )
        finally:
            browser.close()
    print(json.dumps(stats, indent=2))
    for row in conn.execute("SELECT a.job_id, j.company, j.title, a.mode, a.status, a.reason FROM applications a JOIN jobs j ON j.id = a.job_id ORDER BY a.id DESC LIMIT ?", (sum(v for v in stats.values() if isinstance(v, int)),)):
        print(" | ".join(str(value) for value in row))
    return 0


INSTAHYRE_APPLY_PAUSE_SECONDS = 2


def instahyre_apply_command(limit: int) -> int:
    from collections import Counter

    from agent.discover.instahyre import PROFILE_DIR_NAME, apply_on_instahyre, apply_queue, record_apply

    profile_dir = DATA_DIR / PROFILE_DIR_NAME
    if not profile_dir.exists():
        print("instahyre: no saved session; log in once first")
        return 1
    conn = connect(DB_PATH)
    outcomes = Counter()

    def record(job_id: int, outcome: str) -> None:
        record_apply(conn, job_id, outcome)
        outcomes[outcome] += 1

    stopped = apply_on_instahyre(profile_dir, apply_queue(conn, limit), record, INSTAHYRE_APPLY_PAUSE_SECONDS, STOP_FILE)
    left = sum(outcomes.values()) - outcomes["applied"] - outcomes["already"]
    print(json.dumps({"instahyre_applied": outcomes["applied"], "already_applied": outcomes["already"], "left_for_you": left, "stopped": stopped}))
    return 1 if stopped else 0


def instahyre_command(settings: dict, applied_only: bool = False) -> int:
    from agent.discover.instahyre import (
        APPLIED_FACET, PROFILE_DIR_NAME, RECOMMENDED_FACET, InstahyreBlocked, fetch_facets, import_matching,
        mark_applied_on_instahyre, retire_missing,
    )

    profile_dir = DATA_DIR / PROFILE_DIR_NAME
    if not profile_dir.exists():
        print("instahyre: no saved session; log in once first")
        return 1
    facets = (APPLIED_FACET,) if applied_only else (RECOMMENDED_FACET, APPLIED_FACET)
    try:
        lists = fetch_facets(profile_dir, facets)
    except InstahyreBlocked as exc:
        print(f"instahyre: stopped, {exc}; open instahyre.com in your own browser and use the site normally")
        return 1
    conn = connect(DB_PATH)
    now = _stamp(datetime.now())
    marked = mark_applied_on_instahyre(conn, lists[APPLIED_FACET], now)
    if applied_only:
        print(json.dumps({"applied_on_instahyre": len(lists[APPLIED_FACET]), "marked_applied": marked}))
        return 0
    rows = lists[RECOMMENDED_FACET]
    added = import_matching(conn, rows, settings["titles"], now)
    hidden = retire_missing(conn, rows, now)
    print(json.dumps({"instahyre_matches": len(rows), "new_in_tab": added, "hidden_gone": hidden, "marked_applied": marked}))
    return 0


def referrals_command(settings: dict) -> int:
    from agent.referrals import build_referrals

    conn = connect(DB_PATH)
    llm = LLM(conn, settings["daily_budget_usd"])
    brief = profile_brief(load_profile(), load_resume())
    added = build_referrals(conn, llm, settings["models"].get("referral", "sonnet"), brief, _stamp(datetime.now()))
    print(json.dumps({"referral_drafts": added, "llm_spend_today_usd": round(llm.spent_today(), 4)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobagent")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="discover, filter and score jobs")
    run_parser.add_argument("--skip-email", action="store_true")
    run_parser.add_argument("--skip-score", action="store_true")
    apply_parser = commands.add_parser("apply", help="submit applications for ready jobs")
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.add_argument("--limit", type=int)
    commands.add_parser("dashboard", help="serve the read-only dashboard")
    commands.add_parser("check-companies", help="verify every company board responds")
    instahyre = commands.add_parser("instahyre", help="read Instahyre matches into the dashboard tab")
    instahyre.add_argument("--applied", action="store_true", help="only remove jobs already on your Instahyre Applied list")
    instahyre.add_argument("--apply", action="store_true", help="open each job in the Instahyre tab and click Apply")
    instahyre.add_argument("--limit", type=int, default=40)
    commands.add_parser("referrals", help="draft LinkedIn referral messages for India target companies")
    args = parser.parse_args(argv)

    if args.command in ("run", "apply") and STOP_FILE.exists():
        print(f"STOP file present at {STOP_FILE}; exiting without running.")
        return 0

    load_env()
    settings = load_settings()
    if args.command == "dashboard":
        from dashboard.server import serve

        serve(DB_PATH, settings["dashboard"]["host"], settings["dashboard"]["port"])
        return 0
    if args.command == "instahyre" and args.apply:
        return instahyre_apply_command(args.limit)
    if args.command == "instahyre":
        return instahyre_command(settings, args.applied)
    if args.command == "referrals":
        return referrals_command(settings)
    companies = load_companies()
    if args.command == "check-companies":
        return check_companies(companies, settings)
    if args.command == "apply":
        return apply_command(settings, companies, args.dry_run, args.limit)

    conn = connect(DB_PATH)
    now = datetime.now()
    run_id = start_run(conn, _stamp(now))
    llm = LLM(conn, settings["daily_budget_usd"])
    brief = profile_brief(load_profile(), load_resume())
    try:
        with _client(settings) as client:
            stats, errors = run_pipeline(
                conn, settings, companies, brief, llm, client, os.environ.get("GMAIL_APP_PASSWORD"), now,
                skip_email=args.skip_email, skip_score=args.skip_score,
            )
    except Exception as exc:
        finish_run(conn, run_id, _stamp(datetime.now()), {}, [f"crash: {exc.__class__.__name__}: {str(exc)[:200]}"])
        raise
    stats["llm_spend_today_usd"] = round(llm.spent_today(), 4)
    finish_run(conn, run_id, _stamp(datetime.now()), stats, errors)
    print(json.dumps(stats, indent=2))
    for error in errors:
        print(f"error: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
