from playwright.sync_api import Error as PlaywrightError

from agent.filter import title_ok
from agent.models import Job

BASE_URL = "https://www.instahyre.com"


def jobs_from_matching(rows: list[dict], titles_cfg: dict) -> list[Job]:
    jobs = []
    for row in rows:
        job = row["job"]
        if not title_ok(job["title"], titles_cfg)[0]:
            continue
        jobs.append(Job(
            source="instahyre",
            external_id=str(row["id"]),
            company=row["employer"]["company_name"],
            title=job["title"],
            location=job.get("locations") or "",
            url=BASE_URL + job["opportunity_url"],
            description="Skills: " + ", ".join(job.get("keywords") or []),
        ))
    return jobs


PROFILE_DIR_NAME = "instahyre-profile"
MATCHES_PAGE = BASE_URL + "/candidate/opportunities/?matching=true"
MATCHES_API = BASE_URL + "/api/v1/candidate_opportunities/candidate_matching?interest_facet={facet}&limit=30&offset={offset}"
RECOMMENDED_FACET = 0
APPLIED_FACET = 1
CHALLENGE_MARKERS = ("Performing security verification", "Verify you are human", "Just a moment")
MAX_PAGES = 20


class InstahyreBlocked(Exception):
    pass


def blocked_reason(url: str, text: str) -> str:
    if any(marker in text for marker in CHALLENGE_MARKERS):
        return "Cloudflare human check"
    if "/login" in url:
        return "logged out"
    return ""


def import_matching(conn, rows: list[dict], titles_cfg: dict, now: str) -> int:
    from agent.db import update_job, upsert_job

    added = 0
    for job in jobs_from_matching(rows, titles_cfg):
        job_id, new = upsert_job(conn, job, now)
        if new:
            update_job(conn, job_id, status="manual", reason="Instahyre match — apply on Instahyre")
            added += 1
    return added


GONE_REASON = "gone from Instahyre matches (applied there or closed)"


def retire_missing(conn, rows: list[dict], now: str) -> int:
    from agent.db import update_job

    if not rows:
        return 0
    live = {str(row["id"]) for row in rows}
    stale = [row["id"] for row in conn.execute("SELECT id, external_id FROM jobs WHERE source='instahyre' AND status='manual'") if row["external_id"] not in live]
    for job_id in stale:
        update_job(conn, job_id, status="filtered_out", reason=GONE_REASON)
    return len(stale)


APPLIED_REASON = "applied on Instahyre"


def mark_applied_on_instahyre(conn, applied_rows: list[dict], now: str) -> int:
    from agent.db import update_job

    ids = {str(row["id"]) for row in applied_rows}
    names = {(row["employer"]["company_name"].strip().lower(), row["job"]["title"].strip().lower()) for row in applied_rows}
    done = [
        row["id"] for row in conn.execute("SELECT id, external_id, company, title FROM jobs WHERE source='instahyre' AND status='manual'")
        if row["external_id"] in ids or (row["company"].strip().lower(), row["title"].strip().lower()) in names
    ]
    for job_id in done:
        update_job(conn, job_id, status="applied", reason=APPLIED_REASON)
    return len(done)


def fetch_facets(profile_dir, facets: tuple[int, ...], pause_ms: int = 2000) -> dict[int, list[dict]]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile_dir), headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(MATCHES_PAGE, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            reason = blocked_reason(page.url, page.inner_text("body"))
            if reason:
                raise InstahyreBlocked(reason)
            results = {}
            for facet in facets:
                rows = []
                for index in range(MAX_PAGES):
                    data = page.evaluate("url => fetch(url, {credentials: 'include'}).then(r => r.ok ? r.json() : null)", MATCHES_API.format(facet=facet, offset=index * 30))
                    if not isinstance(data, dict):
                        raise InstahyreBlocked("matches API refused the request")
                    objects = data.get("objects") or []
                    rows += objects
                    if not (data.get("meta") or {}).get("next") or not objects:
                        break
                    page.wait_for_timeout(pause_ms)
                results[facet] = rows
            return results
        finally:
            context.close()


AGENT_APPLIED_REASON = "applied on Instahyre by the agent"
APPLY_BUTTON = "button[ng-click='submitChoice(opp, true)']:visible"
SENT_SCRIPT = "() => { const e = document.querySelector('.application-sent'); return !!e && e.offsetParent !== null; }"
APPLY_CONFIRM_SECONDS = 15
TAB_CLOSED = "you closed the Instahyre tab"
WINDOW_CLOSED = "the Instahyre window was closed"
LEFT_REASONS = {
    "unconfirmed": "clicked Apply but Instahyre did not confirm — check this one",
    "no_button": "no Apply button on the Instahyre page — check this one",
    "error": "the Instahyre page did not load properly — check this one",
}


def apply_queue(conn, limit: int) -> list[dict]:
    rows = conn.execute("SELECT id, url, company, title FROM jobs WHERE source='instahyre' AND status='manual' ORDER BY id LIMIT ?", (limit,))
    return [dict(row) for row in rows]


def record_apply(conn, job_id: int, outcome: str) -> None:
    from agent.db import update_job

    if outcome == "applied":
        update_job(conn, job_id, status="applied", reason=AGENT_APPLIED_REASON)
    elif outcome == "already":
        update_job(conn, job_id, status="applied", reason=APPLIED_REASON)
    else:
        update_job(conn, job_id, reason=LEFT_REASONS[outcome])


def apply_one(page, url: str) -> str:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    reason = blocked_reason(page.url, page.inner_text("body"))
    if reason:
        raise InstahyreBlocked(reason)
    if page.evaluate(SENT_SCRIPT):
        return "already"
    button = page.locator(APPLY_BUTTON).first
    if not button.count():
        return "no_button"
    try:
        button.click(timeout=5000)
    except PlaywrightError:
        button.dispatch_event("click")
    for _ in range(APPLY_CONFIRM_SECONDS):
        page.wait_for_timeout(1000)
        if page.evaluate(SENT_SCRIPT):
            return "applied"
    return "unconfirmed"


def apply_jobs(new_page, jobs: list[dict], record, sleep, pause_seconds: float, stop_file) -> str:
    for job in jobs:
        if stop_file.exists():
            return "STOP file"
        try:
            page = new_page()
        except PlaywrightError:
            return WINDOW_CLOSED
        try:
            outcome = apply_one(page, job["url"])
        except InstahyreBlocked as blocked:
            return str(blocked)
        except PlaywrightError as exc:
            if page.is_closed():
                return TAB_CLOSED
            print(f"instahyre: {job['url']}: {str(exc).splitlines()[0]}")
            outcome = "error"
        finally:
            if not page.is_closed():
                page.close()
        record(job["id"], outcome)
        if outcome == "applied":
            sleep(pause_seconds)
    return ""


def apply_on_instahyre(profile_dir, jobs: list[dict], record, pause_seconds: float, stop_file) -> str:
    import time

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile_dir), headless=False)
        try:
            return apply_jobs(context.new_page, jobs, record, time.sleep, pause_seconds, stop_file)
        finally:
            context.close()
