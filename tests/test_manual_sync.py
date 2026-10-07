from datetime import date
from email.message import EmailMessage

from agent.db import update_job, upsert_job
from agent.manual_sync import EXPIRED_REASON, linkedin_applied_id, naukri_applied, sync_manual
from agent.models import Job

from tests.fakes import NOW

TODAY = date(2026, 10, 5)
LINKEDIN_CONFIRMATION = (
    "Your application was sent to Acme\n\nJava Developer\nAcme\nBengaluru\n\n"
    "View job: https://www.linkedin.com/comm/jobs/view/4100000001/?trackingId=x\n\n"
    "Jobs you may be interested in\nBackend Engineer\nOther\n"
    "View job: https://www.linkedin.com/comm/jobs/view/4100000002/\n"
)
NAUKRI_CONFIRMATION = (
    "<html><body><p>Your applications were sent, Good luck!</p><p>Applied on October 05, 2026</p>"
    "<div>Software Engineer</div><div>Krazy Bee Services</div><div>Java Developer</div><div>Beta Corp</div>"
    "<p>Track applications</p><p>Similar jobs for you</p><div>SDE - 1</div><div>Atmos Care</div></body></html>"
)


def email_from(sender: str, subject: str, plain: str = "", html: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    if plain:
        msg.set_content(plain)
    if html:
        msg.add_alternative(html, subtype="html") if plain else msg.set_content(html, subtype="html")
    return msg


def add(conn, source, external_id, title, company, last_seen=NOW):
    job_id, _ = upsert_job(conn, Job(source=source, external_id=external_id, company=company, title=title, location="Bengaluru", url=f"https://example.com/{external_id}"), last_seen)
    update_job(conn, job_id, status="manual")
    return job_id


def status(conn, job_id):
    return conn.execute("SELECT status, reason FROM jobs WHERE id=?", (job_id,)).fetchone()


def test_linkedin_applied_id_takes_the_first_view_job_link_not_the_recommendations():
    assert linkedin_applied_id(LINKEDIN_CONFIRMATION) == "4100000001"
    assert linkedin_applied_id("no links here") == ""


def test_naukri_applied_reads_title_and_company_pairs_between_applied_on_and_track():
    assert naukri_applied(NAUKRI_CONFIRMATION) == [("software engineer", "krazy bee services"), ("java developer", "beta corp")]
    assert naukri_applied("<p>nothing</p>") == []


def test_sync_manual_marks_confirmed_applies_and_leaves_the_rest(conn):
    linked = add(conn, "linkedin", "4100000001", "Java Developer", "Acme")
    recommended = add(conn, "linkedin", "4100000002", "Backend Engineer", "Other")
    naukri = add(conn, "naukri", "n1", "Java Developer", "Beta Corp")
    untouched = add(conn, "naukri", "n2", "SDE - 1", "Atmos Care")
    messages = [
        email_from("LinkedIn <jobs-noreply@linkedin.com>", "Vikas, your application was sent to Acme", plain=LINKEDIN_CONFIRMATION),
        email_from("Naukri <info@naukri.com>", "You applied for 2 jobs on 05 Oct", html=NAUKRI_CONFIRMATION),
    ]
    result = sync_manual(conn, messages, TODAY)
    assert result == {"linkedin_applied": 1, "naukri_applied": 1, "expired": 0}
    assert tuple(status(conn, linked)) == ("applied", "applied on LinkedIn")
    assert tuple(status(conn, naukri)) == ("applied", "applied on Naukri")
    assert status(conn, recommended)["status"] == "manual"
    assert status(conn, untouched)["status"] == "manual"


def test_sync_manual_expires_jobs_not_seen_in_an_alert_for_21_days(conn):
    old = add(conn, "linkedin", "1", "Java Developer", "Acme", last_seen="2026-09-13T10:00:00")
    recent = add(conn, "naukri", "2", "Java Developer", "Beta", last_seen="2026-09-15T10:00:00")
    assert sync_manual(conn, [], TODAY)["expired"] == 1
    assert tuple(status(conn, old)) == ("filtered_out", EXPIRED_REASON)
    assert status(conn, recent)["status"] == "manual"


def test_sync_manual_ignores_instahyre_jobs(conn):
    instahyre = add(conn, "instahyre", "1", "Java Developer", "Acme", last_seen="2026-08-01T10:00:00")
    sync_manual(conn, [], TODAY)
    assert status(conn, instahyre)["status"] == "manual"
