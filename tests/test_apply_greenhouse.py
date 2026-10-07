from datetime import date

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.apply import greenhouse
from agent.apply.greenhouse import GreenhouseDriver


class Response:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"questions": []}


class Client:
    def get(self, url):
        return Response()


class Page:
    def __init__(self, idle=True):
        self.calls = []
        self.idle = idle

    def goto(self, url, **kwargs):
        self.calls.append(("goto", kwargs.get("wait_until")))

    def wait_for_selector(self, selector, **kwargs):
        self.calls.append(("wait_for_selector", selector))

    def wait_for_load_state(self, state, **kwargs):
        self.calls.append(("wait_for_load_state", state))
        if not self.idle:
            raise PlaywrightTimeoutError("Timeout 15000ms exceeded.")

    def screenshot(self, **kwargs):
        pass

    def close(self):
        pass


class Browser:
    def __init__(self, page):
        self.page = page

    def new_page(self, **kwargs):
        return self.page


def fill(page, tmp_path, monkeypatch):
    monkeypatch.setattr(greenhouse, "fill_form", lambda *args: [])
    monkeypatch.setattr(greenhouse, "unfilled_required", lambda page: [])
    driver = GreenhouseDriver(Client(), Browser(page), None, {"location": {"country": "India"}}, {"experience": [{"company": "Acme", "title": "Engineer", "start": "2025-11", "end": "present"}]}, {"Acme": "acme"}, None, date(2026, 10, 7))
    job = {"company": "Acme", "external_id": "1", "title": "Engineer", "url": "", "description": ""}
    return driver(job, tmp_path, live=False)


def test_waits_for_the_form_then_lets_scripts_settle(tmp_path, monkeypatch):
    page = Page()
    assert fill(page, tmp_path, monkeypatch) == ("filled", "")
    assert page.calls == [("goto", "load"), ("wait_for_selector", "#first_name"), ("wait_for_load_state", "networkidle")]


def test_page_that_never_goes_idle_still_fills(tmp_path, monkeypatch):
    assert fill(Page(idle=False), tmp_path, monkeypatch) == ("filled", "")
