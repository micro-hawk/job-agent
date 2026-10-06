import email
import hashlib
import imaplib
import re
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr

from agent.db import is_email_seen, mark_email_seen, upsert_job
from agent.llm import LLMError
from agent.models import Job
from agent.text import html_to_text

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
LINKEDIN_VIEW = re.compile(r"View job:\s*https://www\.linkedin\.com/(?:comm/)?jobs/view/(\d+)")
SEPARATOR = re.compile(r"^-{10,}[ \t]*$", re.M)
LINKEDIN_HEADERS = ("your job alert", "new jobs match", "new jobs from", "manage alerts", "see all jobs")
LINKEDIN_NOISE = re.compile(r"://|<[a-z/]", re.I)
NAUKRI_LINK = re.compile(r'<a\b[^>]*?href="(https://www\.naukri\.com/(?:jd/)?job-listings-[^"?]*?-(\d{9,}))(?:\?[^"]*)?"[^>]*>', re.I)
NAUKRI_YEARS = re.compile(r"-(\d{1,2})-to-(\d{1,2})-years-", re.I)
NAUKRI_MAX_CHARS = 8000
NAUKRI_SYSTEM = (
    "Extract job listings from a Naukri email. Each job is introduced by a [JOB <id>] marker; use that id as job_id, "
    "or an empty string when a job has no marker. Copy title, company, location and salary exactly as written, "
    "using an empty string when absent. Ignore ads, courses, ratings and app promotions."
)
NAUKRI_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "location": {"type": "string"},
                    "salary": {"type": "string"},
                },
                "required": ["job_id", "title", "company", "location", "salary"],
            },
        }
    },
    "required": ["jobs"],
}


def imap_date(day: date) -> str:
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"


def fetch_alert_messages(user: str, password: str, senders: list[str], since: date, is_seen: Callable[[str], bool], timeout: int = 60) -> list[tuple[str, EmailMessage]]:
    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=timeout)
    try:
        imap.login(user, password.replace(" ", ""))
        imap.select("INBOX", readonly=True)
        found = []
        for sender in senders:
            _, data = imap.search(None, f'(SINCE {imap_date(since)} FROM "{sender}")')
            for number in data[0].split():
                _, head = imap.fetch(number, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                message_id = (email.message_from_bytes(head[0][1]).get("Message-ID") or "").strip()
                if not message_id or is_seen(message_id):
                    continue
                _, body = imap.fetch(number, "(BODY.PEEK[])")
                found.append((message_id, email.message_from_bytes(body[0][1], policy=policy.default)))
        return found
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def classify_sender(from_header: str, gmail_cfg: dict) -> str:
    address = parseaddr(from_header or "")[1].lower()
    if address in gmail_cfg["linkedin_senders"]:
        return "linkedin"
    if address.endswith("@" + gmail_cfg["naukri_domain"]) and address not in gmail_cfg["naukri_skip"]:
        return "naukri"
    return "skip"


def message_text(msg: EmailMessage, prefer: str) -> str:
    part = msg.get_body(preferencelist=(prefer,)) or msg.get_body(preferencelist=("plain", "html"))
    return part.get_content() if part else ""


def parse_linkedin(text: str) -> list[Job]:
    jobs = []
    for block in SEPARATOR.split((text or "").replace("\r\n", "\n")):
        match = LINKEDIN_VIEW.search(block)
        if not match:
            continue
        lines = [line.strip() for line in block[: match.start()].splitlines()]
        lines = [line for line in lines if line and not line.lower().startswith(LINKEDIN_HEADERS) and not LINKEDIN_NOISE.search(line)]
        if len(lines) < 3:
            continue
        title, company, location = lines[:3]
        job_id = match.group(1)
        jobs.append(
            Job(
                source="linkedin",
                external_id=job_id,
                company=company,
                title=title,
                location=location,
                remote="remote" in location.lower(),
                url=f"https://www.linkedin.com/jobs/view/{job_id}/",
            )
        )
    return jobs


def naukri_prepare(html_body: str) -> tuple[str, dict[str, str]]:
    urls: dict[str, str] = {}

    def mark(match: re.Match) -> str:
        urls.setdefault(match.group(2), match.group(1))
        return f" [JOB {match.group(2)}] "

    text = html_to_text(NAUKRI_LINK.sub(mark, html_body or ""))
    return text[:NAUKRI_MAX_CHARS], urls


def parse_naukri(html_body: str, llm, model: str) -> list[Job]:
    text, urls = naukri_prepare(html_body)
    if not text.strip():
        return []
    data = llm.call("alert_parse", model, NAUKRI_SYSTEM, text, NAUKRI_SCHEMA)
    jobs = []
    for item in data.get("jobs", []):
        title = (item.get("title") or "").strip()
        company = (item.get("company") or "").strip()
        if not title or not company:
            continue
        url = urls.get((item.get("job_id") or "").strip(), "")
        external_id = item["job_id"].strip() if url else hashlib.sha1(f"{company}|{title}".lower().encode()).hexdigest()[:16]
        years = NAUKRI_YEARS.search(url)
        location = (item.get("location") or "").strip()
        jobs.append(
            Job(
                source="naukri",
                external_id=external_id,
                company=company,
                title=title,
                location=location,
                remote="remote" in location.lower(),
                url=url,
                description=f"{years.group(1)}-{years.group(2)} years experience" if years else "",
                salary_text=(item.get("salary") or "").strip(),
            )
        )
    return jobs


def ingest_alerts(conn: sqlite3.Connection, llm, settings: dict, password: str, today: date, now: str, fetch=fetch_alert_messages) -> tuple[dict, list[str]]:
    gmail = settings["gmail"]
    stats = {"emails": 0, "jobs": 0, "new": 0}
    senders = gmail["linkedin_senders"] + [gmail["naukri_domain"]]
    since = today - timedelta(days=gmail["lookback_days"])
    try:
        messages = fetch(gmail["user"], password, senders, since, lambda message_id: is_email_seen(conn, message_id))
    except (imaplib.IMAP4.error, OSError) as exc:
        return stats, [f"gmail: {exc.__class__.__name__}: {str(exc)[:120]}"]
    errors = []
    for message_id, msg in messages:
        kind = classify_sender(msg.get("From", ""), gmail)
        try:
            if kind == "linkedin":
                jobs = parse_linkedin(message_text(msg, "plain"))
            elif kind == "naukri":
                jobs = parse_naukri(message_text(msg, "html"), llm, settings["models"]["alert_parse"])
            else:
                jobs = []
        except LLMError as exc:
            errors.append(f"{kind} email {message_id}: {exc}")
            continue
        for job in jobs:
            _, is_new = upsert_job(conn, job, now)
            stats["jobs"] += 1
            stats["new"] += int(is_new)
        mark_email_seen(conn, message_id, kind, now)
        if kind != "skip":
            stats["emails"] += 1
    return stats, errors
