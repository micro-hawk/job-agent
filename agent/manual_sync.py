import email
import imaplib
import sqlite3
from datetime import date, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr

from agent.db import update_job
from agent.discover.alerts_email import LINKEDIN_VIEW, imap_date, message_text
from agent.models import ALERT_SOURCES
from agent.text import html_to_text

EXPIRE_DAYS = 21
EXPIRED_REASON = f"expired: not in any alert for {EXPIRE_DAYS} days"
LINKEDIN_SENDER = "jobs-noreply@linkedin.com"
LINKEDIN_SUBJECT = "your application was sent"
NAUKRI_SENDER = "info@naukri.com"
NAUKRI_SUBJECT = "You applied for"
NAUKRI_START = "applied on "
NAUKRI_END = "track applications"
ALL_MAIL = '"[Gmail]/All Mail"'


def linkedin_applied_id(text: str) -> str:
    match = LINKEDIN_VIEW.search(text or "")
    return match.group(1) if match else ""


def naukri_applied(html_body: str) -> list[tuple[str, str]]:
    lines = [line.strip().lower() for line in html_to_text(html_body).splitlines() if line.strip()]
    start = next((index for index, line in enumerate(lines) if line.startswith(NAUKRI_START)), None)
    if start is None or NAUKRI_END not in lines[start:]:
        return []
    block = lines[start + 1 : lines.index(NAUKRI_END, start)]
    return list(zip(block[0::2], block[1::2]))


def fetch_confirmations(user: str, password: str, since: date, timeout: int = 60) -> list[EmailMessage]:
    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=timeout)
    try:
        imap.login(user, password.replace(" ", ""))
        imap.select(ALL_MAIL, readonly=True)
        found = []
        for sender, subject in ((LINKEDIN_SENDER, LINKEDIN_SUBJECT), (NAUKRI_SENDER, NAUKRI_SUBJECT)):
            _, data = imap.search(None, f'(SINCE {imap_date(since)} FROM "{sender}" SUBJECT "{subject}")')
            for number in data[0].split():
                _, body = imap.fetch(number, "(BODY.PEEK[])")
                found.append(email.message_from_bytes(body[0][1], policy=policy.default))
        return found
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def _manual_jobs(conn: sqlite3.Connection, source: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, external_id, company, title FROM jobs WHERE status='manual' AND source=?", (source,)).fetchall()


def sync_manual(conn: sqlite3.Connection, messages: list[EmailMessage], today: date) -> dict:
    linkedin_ids, naukri_pairs = set(), set()
    for msg in messages:
        sender = parseaddr(msg.get("From", ""))[1].lower()
        if sender == LINKEDIN_SENDER:
            linkedin_ids.add(linkedin_applied_id(message_text(msg, "plain")))
        elif sender == NAUKRI_SENDER:
            naukri_pairs.update(naukri_applied(message_text(msg, "html")))
    linkedin = [row["id"] for row in _manual_jobs(conn, "linkedin") if row["external_id"] in linkedin_ids]
    naukri = [row["id"] for row in _manual_jobs(conn, "naukri") if (row["title"].strip().lower(), row["company"].strip().lower()) in naukri_pairs]
    for job_id in linkedin:
        update_job(conn, job_id, status="applied", reason="applied on LinkedIn")
    for job_id in naukri:
        update_job(conn, job_id, status="applied", reason="applied on Naukri")
    cutoff = (today - timedelta(days=EXPIRE_DAYS)).isoformat()
    marks = ",".join("?" * len(ALERT_SOURCES))
    expired = [row[0] for row in conn.execute(f"SELECT id FROM jobs WHERE status='manual' AND source IN ({marks}) AND last_seen < ?", (*ALERT_SOURCES, cutoff))]
    for job_id in expired:
        update_job(conn, job_id, status="filtered_out", reason=EXPIRED_REASON)
    return {"linkedin_applied": len(linkedin), "naukri_applied": len(naukri), "expired": len(expired)}
