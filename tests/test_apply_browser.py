from agent.apply.browser import is_confirmation, parse_security_code


def test_parse_security_code_from_greenhouse_email():
    text = "Hi Alex,\n\nCopy and paste this code into the security code field on your application:\n\nXy7Kp2Qa\n\nThis code expires in 10 minutes."
    assert parse_security_code(text) == "Xy7Kp2Qa"


def test_parse_security_code_absent():
    assert parse_security_code("Thanks for applying to Okta") is None


def test_confirmation_detection():
    assert is_confirmation("https://job-boards.greenhouse.io/embed/job_app/confirmation?for=okta", "")
    assert is_confirmation("https://x", "Thank you for applying. We have received your application.")
    assert is_confirmation("https://x", "Application submitted!")
    assert not is_confirmation("https://job-boards.greenhouse.io/embed/job_app?for=okta&token=1", "Submit application")


def test_multiselect_without_checkboxes_uses_combobox(monkeypatch):
    from agent.apply import browser
    from agent.apply.form import Field

    class Page:
        def locator(self, selector):
            return type("L", (), {"count": lambda self: 0})()

    chosen = []
    monkeypatch.setattr(browser, "_choose", lambda page, name, value: chosen.append((name, value)))
    field = Field("q[]", "Languages", "multiselect", True, ["English", "French"])
    assert browser.fill_form(Page(), [field], {"q[]": ["English"]}, "India") == []
    assert chosen == [("q[]", "English")]


def test_current_employment_from_resume():
    from agent.apply.browser import current_employment

    resume = {"experience": [{"company": "Northwind Payments", "title": "Senior Software Engineer", "start": "2025-11", "end": "present"}]}
    assert current_employment(resume) == {"company": "Northwind Payments", "title": "Senior Software Engineer", "month": "November", "year": "2025"}


def test_optional_field_failure_does_not_block(monkeypatch):
    from agent.apply import browser
    from agent.apply.form import Field

    class Page:
        def locator(self, selector):
            return type("L", (), {"count": lambda self: 0})()

    def boom(page, name, value):
        raise TimeoutError()

    monkeypatch.setattr(browser, "_choose", boom)
    optional = Field("disability_status", "DisabilityStatus", "select", False, ["No"])
    required = Field("q", "Required pick", "select", True, ["A"])
    assert browser.fill_form(Page(), [optional, required], {"disability_status": "No", "q": "A"}, "India") == ["Required pick: TimeoutError"]


EDUCATION_FORM = {"schools": ["Savitribai Phule Pune University", "University of Pune"], "degree": "Bachelor's Degree", "disciplines": ["Information Technology", "Engineering"]}


def test_education_entry_from_profile_and_resume():
    from agent.apply.browser import education_entry

    resume = {"education": [{"institution": "SPPU", "start": "2019-08", "end": "2023-05"}]}
    assert education_entry({"education_form": EDUCATION_FORM}, resume) == {
        **EDUCATION_FORM, "start_year": "2019", "end_month": "May", "end_year": "2023",
    }
    assert education_entry({}, resume) is None


class OptionPage:
    def __init__(self, menus):
        self.menus = menus
        self.typed = None
        self.clicked = []

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        page = self
        if selector == ".select__option":
            return Options(page, [o for o in page.menus.get(page.typed, [])])
        return Box(page)


class Box:
    def __init__(self, page):
        self.page = page
        self.first = self

    def click(self):
        pass

    def fill(self, text):
        self.page.typed = text or None

    def press_sequentially(self, text, delay=0):
        self.page.typed = text


class Options:
    def __init__(self, page, texts):
        self.page = page
        self.texts = texts

    def all_inner_texts(self):
        return self.texts

    def nth(self, index):
        page, text = self.page, self.texts[index]
        return type("O", (), {"click": lambda self, timeout=0: page.clicked.append(text)})()


def test_pick_tries_search_terms_until_one_has_options():
    from agent.apply.browser import _pick

    page = OptionPage({"University of Pune": ["University of Pune"]})
    assert _pick(page, "school--0", ["Savitribai Phule Pune University", "University of Pune"]) == "University of Pune"
    assert page.clicked == ["University of Pune"]


def test_pick_prefers_exact_option_and_raises_when_nothing_matches():
    import pytest
    from agent.apply.browser import _pick

    page = OptionPage({"Engineering": ["Engineering Management", "Engineering"]})
    assert _pick(page, "discipline--0", ["Information Technology", "Engineering"]) == "Engineering"
    with pytest.raises(LookupError):
        _pick(OptionPage({"Other": ["No options"]}), "school--0", ["Nowhere University"])


def test_fill_form_fills_education_when_section_present(monkeypatch):
    from agent.apply import browser

    class Page:
        def locator(self, selector):
            return type("L", (), {"count": lambda self: 1 if "school--0" in selector else 0})()

    seen = []
    monkeypatch.setattr(browser, "_education", lambda page, entry: seen.append(entry))
    entry = {**EDUCATION_FORM, "start_year": "2019", "end_month": "May", "end_year": "2023"}
    assert browser.fill_form(Page(), [], {}, "India", education=entry) == []
    assert seen == [entry]
