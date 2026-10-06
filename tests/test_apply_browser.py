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
