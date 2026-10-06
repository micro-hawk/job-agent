from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone

from agent.apply.security_code import latest_code


def msg(when, body, sender="Greenhouse <no-reply@us.greenhouse-mail.io>"):
    m = EmailMessage()
    m["From"] = sender
    m["Date"] = format_datetime(when)
    m["Subject"] = "Security code for your application"
    m.set_content(body)
    return m


def test_latest_code_after_submit_time():
    started = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc).timestamp()
    messages = [
        msg(datetime(2026, 10, 5, 9, 50, tzinfo=timezone.utc), "application: OLDCODE1"),
        msg(datetime(2026, 10, 5, 10, 1, tzinfo=timezone.utc), "Copy and paste this code into the security code field on your application: NewC0de9"),
        msg(datetime(2026, 10, 5, 10, 2, tzinfo=timezone.utc), "application: SPOOFED1", sender="x@evil.com"),
    ]
    assert latest_code(messages, started) == "NewC0de9"


def test_no_fresh_code():
    started = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc).timestamp()
    assert latest_code([msg(datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc), "application: OLDCODE1")], started) is None


def test_code_from_html_only_email():
    started = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc).timestamp()
    m = EmailMessage()
    m["From"] = "Greenhouse <no-reply@us.greenhouse-mail.io>"
    m["Date"] = format_datetime(datetime(2026, 10, 5, 10, 1, tzinfo=timezone.utc))
    m.set_content('<p>Copy and paste this code into the security code field on your application:</p>\n<h1 style="x">AJ7WQGpY</h1><p>After you enter the code, resubmit.</p>', subtype="html")
    assert latest_code([m], started) == "AJ7WQGpY"
