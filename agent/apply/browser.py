import calendar
import re
import time
from collections.abc import Callable
from pathlib import Path

from agent.apply.form import Field

EMBED_URL = "https://job-boards.greenhouse.io/embed/job_app?for={token}&token={job_id}"
CODE = re.compile(r"(?:security code|code)[^\n]*?:\s*([A-Za-z0-9]{8})\b", re.I)
CONFIRMED_URL = re.compile(r"/confirmation\b")
CONFIRMED_TEXT = re.compile(r"thank you for applying|application (has been )?(submitted|received)|we have received your application", re.I)
SECURITY_INPUT = 'input[id^="security-input"]'
ERROR_SELECTOR = ".helper-text--error:visible, [role=alert]:visible"
CHALLENGE = 'iframe[title*="challenge" i]:visible'
UNFILLED_JS = r"""() => {
  const label = el => {
    if (el.classList.contains('label')) return el.innerText.replace('*', '').trim();
    const id = el.id || (el.name || '');
    const node = document.getElementById(id + '-label') || document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    return ((node && node.innerText) || el.getAttribute('aria-label') || id).replace('*', '').trim();
  };
  const out = [];
  const groups = new Set();
  for (const el of document.querySelectorAll('[aria-required="true"], input[required]')) {
    if (el.getAttribute('aria-hidden') === 'true' || el.type === 'hidden' || el.type === 'file' || el.disabled) continue;
    if (!['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) {
      if (el.classList.contains('file-upload')) {
        if (!/\.(pdf|docx?|txt|rtf)\b/i.test(el.innerText)) out.push(label(el.querySelector('[id^="upload-label"]') || el));
      } else if (el.querySelector('input[type=checkbox]') && !el.querySelector('input[type=checkbox]:checked')) {
        out.push(label(el));
      }
    } else if (el.getAttribute('role') === 'combobox') {
      const control = el.closest('.select__control') || el.closest('.select-shell') || el.parentElement;
      const box = el.closest('[class*="select__container"], .select') || control;
      if (!box.querySelector('.select__single-value, .select__multi-value')) out.push(label(el));
    } else if (el.type === 'checkbox') {
      if (groups.has(el.name)) continue;
      groups.add(el.name);
      if (![...document.getElementsByName(el.name)].some(c => c.checked)) out.push(label(el));
    } else if (!el.value) {
      out.push(label(el));
    }
  }
  return out;
}"""


def parse_security_code(text: str) -> str | None:
    match = CODE.search(text or "")
    return match.group(1) if match else None


def is_confirmation(url: str, text: str) -> bool:
    return bool(CONFIRMED_URL.search(url or "") or CONFIRMED_TEXT.search(text or ""))


def _css_id(name: str) -> str:
    return f'[id="{name}"]'


def _choose(page, name: str, value: str, typed: str | None = None) -> None:
    box = page.locator(_css_id(name)).first
    box.click()
    box.fill(typed or value)
    options = page.locator(".select__option")
    exact = options.filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I))
    target = exact if exact.count() else options.filter(has_text=value)
    target.first.click(timeout=8000)


def _location(page, city: str) -> None:
    box = page.locator(_css_id("candidate-location")).first
    box.click()
    box.press_sequentially(city, delay=60)
    option = page.locator(".select__option").filter(has_text=re.compile(city, re.I))
    option.first.wait_for(timeout=10000)
    option.first.click()


def _checkboxes(page, name: str, values: list[str]) -> None:
    for box in page.locator(f'input[type=checkbox][name="{name}"]').all():
        text = page.locator(f'label[for="{box.get_attribute("id")}"]').inner_text()
        if any(value.lower() in text.lower() for value in values):
            box.check()


def current_employment(resume: dict) -> dict:
    entry = resume["experience"][0]
    year, month = entry["start"].split("-")
    return {"company": entry["company"], "title": entry["title"], "month": calendar.month_name[int(month)], "year": year}


def _employment(page, job: dict) -> None:
    page.locator(_css_id("company-name-0")).fill(job["company"])
    page.locator(_css_id("title-0")).fill(job["title"])
    _choose(page, "start-date-month-0", job["month"])
    page.locator(_css_id("start-date-year-0")).fill(job["year"])
    page.locator(_css_id("current-role-0_1")).check()


NO_OPTIONS = re.compile(r"^\s*(no options|loading\.*)\s*$", re.I)


def education_entry(profile: dict, resume: dict) -> dict | None:
    form = profile.get("education_form")
    if not form or not resume.get("education"):
        return None
    entry = resume["education"][0]
    end_year, end_month = entry["end"].split("-")
    return {**form, "start_year": entry["start"].split("-")[0], "end_month": calendar.month_name[int(end_month)], "end_year": end_year}


def _pick(page, name: str, candidates: list[str]) -> str:
    box = page.locator(_css_id(name)).first
    for candidate in candidates:
        box.click()
        box.fill("")
        box.press_sequentially(candidate, delay=60)
        page.wait_for_timeout(2500)
        options = page.locator(".select__option")
        texts = [text.strip() for text in options.all_inner_texts()]
        if not texts or NO_OPTIONS.match(texts[0]):
            continue
        exact = [i for i, text in enumerate(texts) if text.lower() == candidate.lower()]
        index = exact[0] if exact else 0
        options.nth(index).click(timeout=8000)
        return texts[index]
    box.fill("")
    raise LookupError(f"no option for {candidates}")


def _education(page, entry: dict) -> None:
    _pick(page, "school--0", entry["schools"])
    _pick(page, "degree--0", [entry["degree"]])
    _pick(page, "discipline--0", entry["disciplines"])
    for key, name in (("start_year", "start-year--0"), ("end_year", "end-year--0")):
        if page.locator(_css_id(name)).count():
            page.locator(_css_id(name)).fill(entry[key])
    for name in ("end-month--0", "end-date-month--0"):
        if page.locator(_css_id(name)).count():
            _choose(page, name, entry["end_month"])


def fill_form(page, fields: list[Field], answers: dict, country: str, employment: dict | None = None, education: dict | None = None) -> list[str]:
    problems = []
    if education and page.locator(_css_id("school--0")).count():
        try:
            _education(page, education)
        except Exception as exc:
            problems.append(f"Education: {exc}" if isinstance(exc, LookupError) else f"Education: {exc.__class__.__name__}")
    if employment and page.locator(_css_id("company-name-0")).count():
        try:
            _employment(page, employment)
        except Exception as exc:
            problems.append(f"Employment: {exc.__class__.__name__}")
    if page.locator(_css_id("country")).count():
        try:
            _choose(page, "country", country)
        except Exception as exc:
            problems.append(f"Country: {exc.__class__.__name__}")
    for field in fields:
        value = answers.get(field.name)
        if value is None:
            continue
        try:
            if field.kind == "file":
                page.locator(f'input[type=file][id="{field.name}"]').set_input_files(value)
                page.get_by_text(Path(value).name).first.wait_for(timeout=15000)
            elif field.kind in ("text", "textarea"):
                page.locator(_css_id(field.name)).first.fill(str(value))
            elif field.kind == "select":
                _choose(page, field.name, value)
            elif field.kind == "multiselect":
                if page.locator(f'input[type=checkbox][name="{field.name}"]').count():
                    _checkboxes(page, field.name, value)
                else:
                    for item in value:
                        _choose(page, field.name, item)
            elif field.kind == "location":
                _location(page, value)
            elif field.kind == "checkbox":
                page.locator(f'input[type=checkbox][name="{field.name}"]').first.check()
        except Exception as exc:
            if field.required:
                problems.append(f"{field.label}: {exc.__class__.__name__}")
    return problems


def unfilled_required(page) -> list[str]:
    return page.evaluate(UNFILLED_JS)


def _settle(page, seconds: float, watch_code: bool = True) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_confirmation(page.url, page.inner_text("body")):
            return "confirmed"
        if watch_code and page.locator(SECURITY_INPUT).count():
            return "code"
        if page.locator(CHALLENGE).count():
            return "challenge"
        page.wait_for_timeout(1000)
    return "errors" if page.locator(ERROR_SELECTOR).count() else "timeout"


def submit(page, fetch_code: Callable[[float], str | None], wait_seconds: int = 45) -> tuple[str, str]:
    started = time.time()
    page.locator("button[type=submit]").first.click()
    state = _settle(page, wait_seconds)
    if state == "code":
        code = fetch_code(started)
        if not code:
            return "needs_you", "security code email not found"
        page.locator(SECURITY_INPUT).first.click()
        page.keyboard.type(code, delay=80)
        page.locator("button[type=submit]").first.click()
        state = _settle(page, wait_seconds * 2, watch_code=False)
    if state == "confirmed":
        return "applied", ""
    if state == "challenge":
        return "needs_you", "captcha challenge shown"
    if state == "errors":
        messages = [text.strip() for text in page.locator(ERROR_SELECTOR).all_inner_texts() if text.strip()]
        return "needs_you", "form errors: " + "; ".join(messages[:5])
    return "failed", f"no confirmation after submit ({state})"
