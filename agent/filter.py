import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from agent.db import update_job
from agent.markets import classify_market
from agent.models import ALERT_SOURCES
from agent.salary import SalaryDecision, decide_salary, parse_salary
from agent.text import dedupe_key

ABROAD_MARKETS = {"uk", "eu", "canada", "uae", "singapore"}

_NEGATIVE_SPONSORSHIP = re.compile(
    r"\bno\s+(?:visa\s+)?sponsorship"
    r"|\b(?:not|unable to|cannot|can't|won't|will not|do not|don't|are not able to)\s+(?:provide\s+|offer\s+)?(?:visa\s+|work\s+permit\s+)?sponsor"
    r"|\bmust\s+(?:already\s+)?(?:have|hold)\s+(?:the\s+|a\s+)?(?:existing\s+)?(?:right|authori[sz]ation|eligibility)\s+to\s+work"
    r"|\bwithout\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship"
    r"|\bsponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)"
    r"|\bnot\s+eligible\s+for\s+(?:visa\s+)?sponsorship",
    re.I,
)
_POSITIVE_SPONSORSHIP = re.compile(
    r"\b(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|provided|offered)"
    r"|\bwe\s+(?:will|can|do)\s+sponsor"
    r"|\brelocation\s+(?:support|assistance|package)",
    re.I,
)
_YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*(?:years|yrs)\b(?:\s+of)?(?:\s+[\w/&-]+){0,4}?\s+experience",
    re.I,
)


@dataclass
class FilterResult:
    ok: bool
    reason: str = ""
    market: str | None = None
    salary: SalaryDecision | None = None
    sponsorship: str = "unknown"


def title_ok(title: str, titles_cfg: dict) -> tuple[bool, str]:
    for pattern in titles_cfg["deny"]:
        if re.search(pattern, title, re.I):
            return False, f"title denied: {pattern}"
    if any(re.search(pattern, title, re.I) for pattern in titles_cfg["allow"]):
        return True, ""
    return False, "title not targeted"


def sponsorship_signal(text: str) -> str:
    if _NEGATIVE_SPONSORSHIP.search(text or ""):
        return "negative"
    if _POSITIVE_SPONSORSHIP.search(text or ""):
        return "positive"
    return "unknown"


def min_years_required(text: str) -> int | None:
    values = [int(match.group(1)) for match in _YEARS.finditer(text or "")]
    return min(values) if values else None


def _is_stale(posted_at: str, now: datetime, max_days: int) -> bool:
    if not posted_at:
        return False
    try:
        posted = datetime.fromisoformat(posted_at)
    except ValueError:
        return False
    return (now.date() - posted.date()).days > max_days

MARKET_DOLLARS = {"canada": "CAD", "singapore": "SGD"}


def evaluate(job: Mapping, settings: dict, now: datetime) -> FilterResult:
    ok, reason = title_ok(job["title"], settings["titles"])
    if not ok:
        return FilterResult(False, reason)
    if _is_stale(job["posted_at"], now, settings["max_job_age_days"]):
        return FilterResult(False, f"posted more than {settings['max_job_age_days']} days ago")
    default_country = "india" if job["source"] == "naukri" else None
    market = classify_market(job["location"], job["description"], bool(job["remote"]), default_country)
    if market.market is None:
        return FilterResult(False, market.reason)
    sponsorship = sponsorship_signal(job["description"])
    if market.market in ABROAD_MARKETS and sponsorship == "negative":
        return FilterResult(False, "no visa sponsorship", market.market, sponsorship=sponsorship)
    enabled = settings.get("markets")
    if enabled is not None and market.market not in enabled:
        return FilterResult(False, f"market not targeted: {market.market}", market.market, sponsorship=sponsorship)
    years = min_years_required(job["description"])
    if years is not None and years > settings["max_required_years"]:
        return FilterResult(False, f"requires {years}+ years", market.market, sponsorship=sponsorship)
    dollar = MARKET_DOLLARS.get(market.market, "USD")
    salary_range = parse_salary(job["salary_text"], dollar=dollar) or parse_salary(job["description"], require_range=True, dollar=dollar)
    decision = decide_salary(market.market, salary_range, job["company_tier"], settings["salary"])
    if decision.skip:
        return FilterResult(False, decision.reason, market.market, decision, sponsorship)
    return FilterResult(True, "", market.market, decision, sponsorship)


def run_filters(conn: sqlite3.Connection, settings: dict, now: datetime) -> dict[str, int]:
    counts = {"candidate": 0, "manual": 0, "filtered_out": 0}
    since = (now - timedelta(days=settings["dedupe_days"])).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status='discovered' ORDER BY CASE WHEN source IN ('linkedin','naukri') THEN 1 ELSE 0 END, id"
    ).fetchall()
    for job in rows:
        key = dedupe_key(job["company"], job["title"])
        result = evaluate(job, settings, now)
        if result.ok:
            duplicate = conn.execute(
                "SELECT id FROM jobs WHERE dedupe_key=? AND id!=? AND status NOT IN ('discovered','filtered_out') AND first_seen>=? LIMIT 1",
                (key, job["id"], since),
            ).fetchone()
            if duplicate:
                result = replace(result, ok=False, reason=f"duplicate of #{duplicate['id']}")
        if result.ok:
            status = "manual" if job["source"] in ALERT_SOURCES else "candidate"
        else:
            status = "filtered_out"
        update_job(
            conn, job["id"], status=status, reason=result.reason, dedupe_key=key, market=result.market,
            sponsorship=result.sponsorship, expected_salary=result.salary.text if result.salary and not result.salary.skip else None,
        )
        counts[status] += 1
    return counts
