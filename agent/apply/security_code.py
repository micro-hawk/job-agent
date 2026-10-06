import email
import imaplib
import time
from datetime import date
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

from agent.apply.browser import parse_security_code
from agent.discover.alerts_email import imap_date, message_text
from agent.text import html_to_text

SENDER_DOMAIN = "greenhouse-mail.io"
CLOCK_SKEW_SECONDS = 120


def latest_code(messages: list[EmailMessage], started: float) -> str | None:
    fresh = []
    for msg in messages:
        if not parseaddr(msg["From"] or "")[1].lower().endswith(SENDER_DOMAIN):
            continue
        sent = parsedate_to_datetime(msg["Date"]).timestamp()
        if sent >= started - CLOCK_SKEW_SECONDS:
            fresh.append((sent, msg))
    for _, msg in sorted(fresh, key=lambda item: item[0], reverse=True):
        code = parse_security_code(html_to_text(message_text(msg, "plain")))
        if code:
            return code
    return None


def fetch_recent(user: str, password: str, day: date, timeout: int = 60) -> list[EmailMessage]:
    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=timeout)
    try:
        imap.login(user, password.replace(" ", ""))
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, f'(SINCE {imap_date(day)} FROM "{SENDER_DOMAIN}")')
        found = []
        for number in data[0].split()[-5:]:
            _, body = imap.fetch(number, "(BODY.PEEK[])")
            found.append(email.message_from_bytes(body[0][1], policy=policy.default))
        return found
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def code_fetcher(user: str, password: str, wait_seconds: int = 300, poll_seconds: int = 10):
    def fetch(started: float) -> str | None:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            time.sleep(poll_seconds)
            code = latest_code(fetch_recent(user, password, date.today()), started)
            if code:
                return code
        return None

    return fetch
