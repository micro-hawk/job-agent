import json
from datetime import date
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.apply.answers import answer_fields
from agent.apply.browser import EMBED_URL, current_employment, education_entry, fill_form, submit, unfilled_required
from agent.apply.form import parse_greenhouse_form

FORM_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true"
FORM_READY = "#first_name"


class GreenhouseDriver:
    def __init__(self, client, browser, llm, profile: dict, resume: dict, tokens: dict[str, str], fetch_code, today: date):
        self.client = client
        self.browser = browser
        self.llm = llm
        self.profile = profile
        self.resume = resume
        self.tokens = tokens
        self.fetch_code = fetch_code
        self.today = today

    def __call__(self, job, app_dir: Path, live: bool) -> tuple[str, str]:
        token = self.tokens.get(job["company"])
        if not token:
            return "needs_you", f"no greenhouse token for {job['company']}"
        response = self.client.get(FORM_URL.format(token=token, job_id=job["external_id"]))
        if response.status_code == 404:
            return "failed", "posting closed"
        response.raise_for_status()
        fields = parse_greenhouse_form(response.json())
        (app_dir / "jd.md").write_text(f"# {job['title']} — {job['company']}\n\n{job['url']}\n\n{job['description'] or ''}\n")
        saved = app_dir / "answers.json"
        prefilled = json.loads(saved.read_text())["answers"] if saved.exists() else {}
        answers, missing = answer_fields(fields, dict(job), self.profile, self.resume, self.llm, self.today, prefilled)
        saved.write_text(json.dumps({"answers": answers, "missing": missing}, indent=2, ensure_ascii=False))
        if missing:
            return "needs_you", "unanswered required: " + "; ".join(label[:80] for label in missing)
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(EMBED_URL.format(token=token, job_id=job["external_id"]), wait_until="load", timeout=60000)
            page.wait_for_selector(FORM_READY, timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            problems = fill_form(page, fields, answers, self.profile["location"]["country"], current_employment(self.resume), education_entry(self.profile, self.resume))
            unfilled = unfilled_required(page)
            page.screenshot(path=str(app_dir / "filled.png"), full_page=True)
            if problems or unfilled:
                return "needs_you", "could not fill: " + "; ".join((problems + unfilled)[:6])
            if not live:
                return "filled", ""
            status, reason = submit(page, self.fetch_code)
            page.screenshot(path=str(app_dir / "after_submit.png"), full_page=True)
            return status, reason
        finally:
            page.close()
