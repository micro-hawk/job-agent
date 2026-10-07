from agent.config import EXAMPLE_DIR, load_settings
from agent.discover.instahyre import jobs_from_matching

ROW = {
    "id": "6212473034",
    "employer": {"company_name": "Piramal Finance Limited"},
    "job": {"title": "SDE II", "locations": "Bangalore", "opportunity_url": "/job-1-sde-ii-at-piramal/", "keywords": ["Java", "Kafka"]},
}


def test_jobs_from_matching_keeps_targeted_titles():
    other = {**ROW, "id": "2", "job": {**ROW["job"], "title": "Engineering Manager"}}
    jobs = jobs_from_matching([ROW, other], load_settings(EXAMPLE_DIR)["titles"])
    assert len(jobs) == 1
    job = jobs[0]
    assert (job.source, job.external_id, job.company, job.title, job.location) == ("instahyre", "6212473034", "Piramal Finance Limited", "SDE II", "Bangalore")
    assert job.url == "https://www.instahyre.com/job-1-sde-ii-at-piramal/"
    assert "Java, Kafka" in job.description


def test_import_matching_adds_new_jobs_as_manual_once(conn):
    from agent.discover.instahyre import import_matching
    titles = load_settings(EXAMPLE_DIR)["titles"]
    assert import_matching(conn, [ROW], titles, "2026-10-06T10:00:00") == 1
    assert import_matching(conn, [ROW], titles, "2026-10-07T10:00:00") == 0
    row = conn.execute("SELECT status, source FROM jobs").fetchone()
    assert (row["status"], row["source"]) == ("manual", "instahyre")


def test_blocked_reason_detects_challenge_and_logout():
    from agent.discover.instahyre import blocked_reason
    assert blocked_reason("https://www.instahyre.com/candidate/opportunities/", "Performing security verification") == "Cloudflare human check"
    assert blocked_reason("https://www.instahyre.com/login/", "") == "logged out"
    assert blocked_reason("https://www.instahyre.com/candidate/opportunities/?matching=true", "Opportunities") == ""


def test_retire_missing_hides_matches_gone_from_instahyre(conn):
    from agent.discover.instahyre import import_matching, retire_missing
    titles = load_settings(EXAMPLE_DIR)["titles"]
    gone = {**ROW, "id": "2", "job": {**ROW["job"], "opportunity_url": "/job-2/"}}
    applied = {**ROW, "id": "3", "job": {**ROW["job"], "opportunity_url": "/job-3/"}}
    import_matching(conn, [ROW, gone, applied], titles, "2026-10-06T10:00:00")
    conn.execute("UPDATE jobs SET status='applied' WHERE external_id='3'")
    assert retire_missing(conn, [], "2026-10-07T10:00:00") == 0
    assert retire_missing(conn, [ROW], "2026-10-07T10:00:00") == 1
    statuses = {row["external_id"]: (row["status"], row["reason"]) for row in conn.execute("SELECT external_id, status, reason FROM jobs")}
    assert statuses["6212473034"][0] == "manual"
    assert statuses["2"] == ("filtered_out", "gone from Instahyre matches (applied there or closed)")
    assert statuses["3"][0] == "applied"


def test_mark_applied_on_instahyre_matches_by_id_or_company_and_title(conn):
    from agent.discover.instahyre import import_matching, mark_applied_on_instahyre
    titles = load_settings(EXAMPLE_DIR)["titles"]
    relisted = {**ROW, "id": "2", "employer": {"company_name": "Zluri"}, "job": {**ROW["job"], "title": "Senior Backend Engineer", "opportunity_url": "/job-2/"}}
    untouched = {**ROW, "id": "3", "employer": {"company_name": "Loco"}, "job": {**ROW["job"], "opportunity_url": "/job-3/"}}
    import_matching(conn, [ROW, relisted, untouched], titles, "2026-10-06T10:00:00")
    applied_rows = [ROW, {**relisted, "id": "999", "employer": {"company_name": "zluri"}, "job": {**relisted["job"], "title": "senior backend engineer"}}]
    assert mark_applied_on_instahyre(conn, applied_rows, "2026-10-07T10:00:00") == 2
    assert mark_applied_on_instahyre(conn, applied_rows, "2026-10-07T11:00:00") == 0
    statuses = {row["external_id"]: (row["status"], row["reason"]) for row in conn.execute("SELECT external_id, status, reason FROM jobs")}
    assert statuses["6212473034"] == ("applied", "applied on Instahyre")
    assert statuses["2"] == ("applied", "applied on Instahyre")
    assert statuses["3"][0] == "manual"


class FakeButton:
    def __init__(self, page):
        self.page = page
        self.first = self

    def count(self):
        return 1 if self.page.state == "open" else 0

    def is_visible(self):
        return self.page.state == "open"

    def click(self, timeout=None):
        if self.page.intercepted:
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
            raise PlaywrightTimeout("intercepts pointer events")
        self.dispatch_event("click")

    def dispatch_event(self, event):
        self.page.clicks += 1
        self.page.state = self.page.after_click


class FakePage:
    def __init__(self, state, after_click="sent", text="Opportunities", url="https://www.instahyre.com/job-1/"):
        self.state, self.after_click, self.text, self.url = state, after_click, text, url
        self.clicks = 0
        self.closed = False
        self.intercepted = False
        self.broken = False
        self.user_closes = False

    def goto(self, url, wait_until=None):
        if self.user_closes:
            from playwright.sync_api import Error
            self.closed = True
            raise Error("Target page, context or browser has been closed")
        if self.broken:
            from playwright.sync_api import Error
            raise Error("Page.goto: Timeout 30000ms exceeded")
        self.visited = url

    def wait_for_timeout(self, ms):
        if getattr(self, "urls_after_poll", None):
            self.url = self.urls_after_poll.pop(0)

    def inner_text(self, selector):
        return self.text

    def evaluate(self, script):
        return self.state == "sent"

    def locator(self, selector):
        return FakeButton(self)

    def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


def test_apply_one_clicks_apply_and_waits_for_confirmation():
    from agent.discover.instahyre import apply_one
    page = FakePage("open")
    assert apply_one(page, "https://www.instahyre.com/job-1/") == "applied" and page.clicks == 1
    assert apply_one(FakePage("sent"), "u") == "already"
    assert apply_one(FakePage("missing"), "u") == "no_button"
    assert apply_one(FakePage("open", after_click="open"), "u") == "unconfirmed"


def test_apply_one_falls_back_to_dispatched_click_when_button_is_covered():
    from agent.discover.instahyre import apply_one
    page = FakePage("open")
    page.intercepted = True
    assert apply_one(page, "u") == "applied" and page.clicks == 1


def test_apply_jobs_leaves_a_job_that_errors_and_moves_on(tmp_path):
    from agent.discover.instahyre import apply_jobs
    broken = FakePage("open")
    broken.broken = True
    pages = iter([broken, FakePage("open")])
    results = []
    stopped = apply_jobs(lambda: next(pages), [{"id": 1, "url": "u"}, {"id": 2, "url": "v"}], lambda job_id, outcome: results.append((job_id, outcome)), lambda s: None, 10, tmp_path / "STOP")
    assert stopped == "" and results == [(1, "error"), (2, "applied")] and broken.closed


def test_apply_one_stops_on_challenge():
    import pytest
    from agent.discover.instahyre import InstahyreBlocked, apply_one
    page = FakePage("open", text="Performing security verification")
    with pytest.raises(InstahyreBlocked):
        apply_one(page, "u")
    assert page.clicks == 0


def test_apply_jobs_uses_a_tab_per_job_records_results_and_stops_when_blocked(tmp_path):
    from agent.discover.instahyre import apply_jobs
    pages = [FakePage("open"), FakePage("sent"), FakePage("open", text="Just a moment"), FakePage("open")]
    opened = iter(pages)
    results, sleeps = [], []
    jobs = [{"id": n, "url": f"u{n}"} for n in range(4)]
    stopped = apply_jobs(lambda: next(opened), jobs, lambda job_id, outcome: results.append((job_id, outcome)), sleeps.append, 10, tmp_path / "STOP")
    assert results == [(0, "applied"), (1, "already")]
    assert stopped == "Cloudflare human check"
    assert all(page.closed for page in pages[:3]) and pages[3].clicks == 0
    assert sleeps == [10]


def test_apply_jobs_honours_stop_file(tmp_path):
    from agent.discover.instahyre import apply_jobs
    (tmp_path / "STOP").touch()
    assert apply_jobs(lambda: FakePage("open"), [{"id": 1, "url": "u"}], lambda *a: None, lambda s: None, 10, tmp_path / "STOP") == "STOP file"


def test_apply_queue_and_record_apply(conn):
    from agent.discover.instahyre import apply_queue, import_matching, record_apply
    titles = load_settings(EXAMPLE_DIR)["titles"]
    rows = [{**ROW, "id": str(n), "job": {**ROW["job"], "opportunity_url": f"/job-{n}/"}} for n in range(4)]
    import_matching(conn, rows, titles, "2026-10-06T10:00:00")
    queue = apply_queue(conn, 3)
    assert len(queue) == 3
    for job, outcome in zip(queue, ("applied", "already", "error")):
        record_apply(conn, job["id"], outcome)
    found = {row["external_id"]: (row["status"], row["reason"]) for row in conn.execute("SELECT external_id, status, reason FROM jobs")}
    assert found["0"] == ("applied", "applied on Instahyre by the agent")
    assert found["1"] == ("applied", "applied on Instahyre")
    assert found["2"] == ("manual", "the Instahyre page did not load properly — check this one")
    assert found["3"][0] == "manual"
    assert [job["id"] for job in apply_queue(conn, 10)] == [queue[2]["id"], *[r["id"] for r in conn.execute("SELECT id FROM jobs WHERE external_id='3'")]]


def test_apply_jobs_stops_when_you_close_the_tab(tmp_path):
    from agent.discover.instahyre import apply_jobs
    closing = FakePage("open")
    closing.user_closes = True
    pages = iter([closing, FakePage("open")])
    results = []
    stopped = apply_jobs(lambda: next(pages), [{"id": 1, "url": "u"}, {"id": 2, "url": "v"}], lambda *a: results.append(a), lambda s: None, 10, tmp_path / "STOP")
    assert stopped == "you closed the Instahyre tab" and results == []


def test_apply_jobs_stops_when_the_window_is_gone(tmp_path):
    from playwright.sync_api import Error

    from agent.discover.instahyre import apply_jobs

    def gone():
        raise Error("Target page, context or browser has been closed")

    assert apply_jobs(gone, [{"id": 1, "url": "u"}], lambda *a: None, lambda s: None, 10, tmp_path / "STOP") == "the Instahyre window was closed"


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.pages = [page]
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


def fake_launcher(*pages):
    queue = list(pages)
    launches = []

    def launch(headless):
        context = FakeContext(queue.pop(0))
        launches.append((headless, context))
        return context

    return launch, launches


LOGIN_URL = "https://www.instahyre.com/login/"


def test_open_session_stays_hidden_when_signed_in():
    from agent.discover.instahyre import open_session
    launch, launches = fake_launcher(FakePage("open"))
    context, page = open_session(launch, login_polls=3)
    assert [headless for headless, _ in launches] == [True]
    assert context is launches[0][1] and not context.closed


def test_open_session_shows_a_window_only_to_sign_in_then_goes_hidden():
    from agent.discover.instahyre import open_session
    login = FakePage("open", url=LOGIN_URL)
    signing_in = FakePage("open", url=LOGIN_URL)
    signing_in.urls_after_poll = ["https://www.instahyre.com/candidate/opportunities/"]
    launch, launches = fake_launcher(login, signing_in, FakePage("open"))
    context, _ = open_session(launch, login_polls=3)
    assert [headless for headless, _ in launches] == [True, False, True]
    assert launches[0][1].closed and launches[1][1].closed and not context.closed


def test_open_session_gives_up_when_you_do_not_sign_in():
    import pytest
    from agent.discover.instahyre import InstahyreBlocked, open_session
    launch, launches = fake_launcher(FakePage("open", url=LOGIN_URL), FakePage("open", url=LOGIN_URL))
    with pytest.raises(InstahyreBlocked, match="not signed in"):
        open_session(launch, login_polls=3)
    assert all(context.closed for _, context in launches)


def test_open_session_falls_back_to_a_window_when_hidden_run_is_challenged_and_never_passes_a_challenge():
    import pytest
    from agent.discover.instahyre import InstahyreBlocked, open_session
    launch, launches = fake_launcher(FakePage("open", text="Just a moment"), FakePage("open"))
    context, _ = open_session(launch, login_polls=3)
    assert [headless for headless, _ in launches] == [True, False] and not context.closed

    launch, launches = fake_launcher(FakePage("open", text="Just a moment"), FakePage("open", text="Just a moment"))
    with pytest.raises(InstahyreBlocked, match="Cloudflare"):
        open_session(launch, login_polls=3)
    assert all(context.closed for _, context in launches)
