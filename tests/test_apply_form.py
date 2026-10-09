import json
from datetime import date
from pathlib import Path

from agent.config import EXAMPLE_DIR
from agent.apply.answers import answer_fields
from agent.apply.form import Field, parse_greenhouse_form
from agent.profile import load_profile, load_resume

from tests.fakes import StubLLM

FIXTURES = Path(__file__).parent / "fixtures"
PROFILE = load_profile(EXAMPLE_DIR / "profile.yaml")
RESUME = load_resume(EXAMPLE_DIR / "master_resume.yaml")
TODAY = date(2026, 10, 5)


def form(name):
    return parse_greenhouse_form(json.loads((FIXTURES / f"{name}.json").read_text()))


def by_name(fields):
    return {f.name: f for f in fields}


def job(company, market="india"):
    return {"id": 1, "company": company, "title": "Senior Software Engineer", "market": market, "description": "Java, Kafka", "location": "Bengaluru"}


def test_parse_maps_question_types():
    fields = by_name(form("gh_mongodb_8178900"))
    assert fields["first_name"] == Field("first_name", "First Name", "text", True, [])
    assert fields["resume"].kind == "file" and fields["resume"].required
    assert "resume_text" not in fields and "cover_letter_text" not in fields
    assert fields["question_68987092"].kind == "select"
    assert fields["question_68987092"].options == ["Yes", "No"]
    assert fields["candidate-location"].kind == "location"
    assert "latitude" not in fields
    assert fields["32288"].kind == "select" and fields["32288"].required
    assert fields["gdpr_demographic_data_consent_given"].kind == "checkbox"


def test_parse_compliance_and_multiselect():
    fields = by_name(form("gh_okta_8222464"))
    assert fields["question_69355519[]"].kind == "multiselect"
    assert fields["question_69355519[]"].options == ["I acknowledge"]
    assert fields["gender"].kind == "select" and not fields["gender"].required
    assert "Asian" in fields["race"].options


def test_known_answers_for_india_job():
    fields = form("gh_mongodb_8178900")
    answers, missing = answer_fields(fields, job("MongoDB"), PROFILE, RESUME, StubLLM({}), TODAY)
    assert missing == []
    assert answers["first_name"] == "Alex"
    assert answers["email"] == "alex.morgan@example.com"
    assert answers["resume"] == PROFILE["resume_pdf"]
    assert answers["question_68987085"] == "Alex"
    assert answers["question_68987087"] == PROFILE["links"]["linkedin"]
    assert answers["question_68987092"] == "No"
    assert answers["question_68987093"] == "No"
    assert answers["question_68987088"] == "No"
    assert answers["candidate-location"] == "Bengaluru"
    assert answers["32288"] == "Male"
    assert answers["gdpr_demographic_data_consent_given"] is True
    assert "cover_letter" not in answers


def test_sponsorship_is_truthful_abroad():
    fields = form("gh_okta_8222464")
    answers, missing = answer_fields(fields, job("Okta", market="canada"), PROFILE, RESUME, StubLLM({}), TODAY)
    assert missing == []
    assert answers["question_69355512"] == "Yes"
    assert answers["question_69355513"] == "Yes"
    assert answers["question_69355514"] == "No"
    assert answers["question_69355516"] == "No"
    assert answers["question_69355518"] == "No"
    assert answers["question_69355519[]"] == ["I acknowledge"]
    assert answers["race"] == "Asian"
    assert answers["veteran_status"] == "I am not a protected veteran"


def test_free_text_questions_use_grounded_llm():
    llm = StubLLM({"answer": lambda prompt: {"answer": "Yes, Kafka-based event-driven services at Northwind Payments."}})
    fields = form("gh_toast_8184675")
    answers, missing = answer_fields(fields, job("Toast"), PROFILE, RESUME, llm, TODAY)
    assert missing == []
    assert answers["question_69205975"] == "4"
    assert answers["question_69205985"] == "Yes"
    assert answers["question_69031374"] == PROFILE["links"]["linkedin"]
    assert answers["question_69031378"] == "I agree"
    assert answers["question_69205981"].startswith("Yes, Kafka")
    assert all(call[0] == "answer" and call[1] == "sonnet" for call in llm.calls)
    assert "Do you have experience in working in Distributed systems?" in llm.calls[-1][2] or llm.calls


def test_unanswerable_required_question_is_missing():
    llm = StubLLM({"answer": lambda prompt: {"answer": None}, "choose": lambda prompt: {"option": None}})
    fields = [
        Field("q1", "What is your favourite colour?", "text", True, []),
        Field("q2", "Pick a shift", "select", True, ["Night", "Day"]),
        Field("q3", "Optional essay", "textarea", False, []),
    ]
    answers, missing = answer_fields(fields, job("X"), PROFILE, RESUME, llm, TODAY)
    assert answers == {}
    assert missing == ["What is your favourite colour?", "Pick a shift"]


def test_llm_choice_must_be_a_listed_option():
    llm = StubLLM({"choose": lambda prompt: {"option": "Evening"}})
    fields = [Field("q2", "Pick a shift", "select", True, ["Night", "Day"])]
    answers, missing = answer_fields(fields, job("X"), PROFILE, RESUME, llm, TODAY)
    assert missing == ["Pick a shift"]


def test_how_heard_prefers_company_careers_page():
    fields = by_name(form("gh_mongodb_8178900"))
    answers, _ = answer_fields([fields["question_68987089"]], job("MongoDB"), PROFILE, RESUME, StubLLM({}), TODAY)
    assert answers["question_68987089"] == "MongoDB Careers Page"
    generic = Field("h", "How did you hear about us?", "select", False, ["College Careers Page", "Referral", "Other"])
    answers, _ = answer_fields([generic], job("Acme"), PROFILE, RESUME, StubLLM({}), TODAY)
    assert answers["h"] == "Other"


def test_hispanic_question_precedes_race():
    names = [f.name for f in form("gh_okta_8222464")]
    assert names.index("hispanic_ethnicity") == names.index("race") - 1
    answers, _ = answer_fields(form("gh_okta_8222464"), job("Okta", "canada"), PROFILE, RESUME, StubLLM({}), TODAY)
    assert answers["hispanic_ethnicity"] == "No"


def test_prefilled_answers_skip_llm():
    llm = StubLLM({})
    fields = [Field("q1", "Describe your Kafka work", "textarea", True, [])]
    answers, missing = answer_fields(fields, job("X"), PROFILE, RESUME, llm, TODAY, prefilled={"q1": "Kafka sync service."})
    assert answers == {"q1": "Kafka sync service."} and missing == [] and llm.calls == []


def _answer(field, company="Acme", market="eu", location="Paris, France"):
    answers, missing = answer_fields([field], {**job(company, market), "location": location}, PROFILE, RESUME, StubLLM({}), TODAY)
    return answers.get(field.name)


def test_common_multiselects():
    assert _answer(Field("c[]", "How should we communicate with you?", "multiselect", True, ["Email", "Phone Call", "WhatsApp"])) == ["Email"]
    assert _answer(Field("l[]", "Please select all the languages you speak fluently.", "multiselect", True, ["Dutch", "English", "French"])) == ["English"]
    assert _answer(Field("w[]", "In what cities are you available to work?", "multiselect", True, ["Amsterdam", "Bangalore", "Paris"])) == ["Paris"]
    heard = Field("h[]", "How did you learn about this job?", "multiselect", True, ["Linkedin", "Zscaler Careers Page", "Other"])
    assert _answer(heard, "Zscaler", "india") == ["Zscaler Careers Page"]


def test_common_selects():
    assert _answer(Field("s", "Sex", "select", True, ["Male", "Female", "I don't wish to answer"])) == "Male"
    assert _answer(Field("e", "May we contact your current employer?", "select", True, ["Yes", "No"])) == "No"
    notice = "I acknowledge that I have read and understood the terms of the Lyft Candidate Privacy Notice."
    assert _answer(Field("d", "Please review the linked document:", "select", True, [notice])) == notice
    assert _answer(Field("p", "I confirm, that I have read the Celonis Privacy Notice", "select", True, ["I confirm"])) == "I confirm"
    office = Field("o", "What is your preferred office location?", "select", True, ["Menlo Park, CA", "Toronto, Canada"])
    assert _answer(office, market="canada", location="Toronto, Canada") == "Toronto, Canada"


def test_current_location_select_matches_city_alias():
    cities = ["Pune", "Gurugram\xa0", "Hyderabad", "Bangalore\xa0", "Other\xa0"]
    assert _answer(Field("l", "What is your current job location ?", "select", True, cities)) == "Bangalore\xa0"
    assert _answer(Field("l", "Where are you currently located?", "select", True, ["Bengaluru", "Pune"])) == "Bengaluru"


def test_current_compensation_select_picks_range():
    ranges = ["0 to 10,00,000", "11,00,000 to 20,00,000", "20,00,000 to 30,00,000"]
    assert _answer(Field("c", "What is your current compensation ?", "select", True, ranges)) == "11,00,000 to 20,00,000"
    lpa = ["Below 10 LPA", "10-15 LPA", "15-20 LPA", "20+ LPA"]
    assert _answer(Field("c", "Current CTC", "select", True, lpa)) == "15-20 LPA"
    low = Field("c", "Current CTC", "select", True, ["0 to 5,00,000", "5,00,000 to 10,00,000"])
    answers, missing = answer_fields([low], job("Acme"), PROFILE, RESUME, StubLLM({"choose": lambda prompt: {"option": None}}), TODAY)
    assert "c" not in answers and missing == ["Current CTC"]


def test_salary_expectation_and_signature():
    assert _answer(Field("x", "Salary Expectation", "text", True)) == PROFILE["expected_salary"]["eu"]
    signature = Field("sig", "I certify that the facts set forth in this Application for Employment are true", "text", True)
    assert _answer(signature) == "Alex Morgan, 2026-10-05"


def test_address_and_postal_code_from_profile():
    assert _answer(Field("z", "Zip / postal code", "text", True)) == "560001"
    assert _answer(Field("p", "What's your postal code?", "text", True)) == "560001"
    assert _answer(Field("a", "Home Address", "text", True)) == "12 Example Street, Bengaluru, Karnataka, India"


def test_skill_multiselects_use_resume_skills():
    langs = Field("l[]", "Which of the following programming languages do you have professional experience writing production code in? (Select all that apply)", "multiselect", True, ["Python", "Golang", "Kotlin", "Java", "Other", "None"])
    assert _answer(langs) == ["Python", "Java"]
    brokers = Field("b[]", "Which of the following have you worked with professionally in a production environment?", "multiselect", True, ["Kafka", "RabbitMQ", "AWS SQS / SNS", "None of the above"])
    assert _answer(brokers) == ["Kafka"]


def test_address_line_and_availability():
    assert _answer(Field("a1", "Address Line 1", "text", True)) == PROFILE["location"]["address"]
    assert _answer(Field("av", "When is your earliest availability?", "text", True)).startswith("Serving notice")


def test_restrictive_agreements_from_declarations():
    airbnb = Field("n", "Are you currently subject to any non-compete or non-solicitation agreement that would impact your ability to work at Airbnb?", "select", True, ["Yes", "No"])
    scale = Field("m", "Are you currently bound by any agreements with a current or former employer that may restrict your ability to work for Scale AI?", "select", True, ["Yes", "No"])
    assert _answer(airbnb) == "No" and _answer(scale) == "No"


def test_multi_select_demographic_question():
    data = {"demographic_questions": {"questions": [{"id": 353, "type": "multi_value_multi_select", "required": True, "label": "Voluntary Self-Identification of Gender",
                                                     "answer_options": [{"label": "Male"}, {"label": "Female"}, {"label": "I don't wish to answer"}]}]}}
    (field,) = parse_greenhouse_form(data)
    assert (field.name, field.kind) == ("353", "multiselect")
    assert _answer(field) == ["Male"]


def test_second_batch_screening_questions():
    heard = Field("h", "How did you hear about us?", "select", True, ["Glassdoor", "LinkedIn", "Other (Please specify below)", "Starburst blog"])
    assert _answer(heard, "Starburst") == "LinkedIn"
    assert _answer(Field("w", "Are you able to legally work in the region you are applying for?", "select", True, ["Yes", "No"]), market="uk") == "No"
    assert _answer(Field("r", "Are you comfortable with our recruiting team using BrightHire, a tool that records and transcribes interviews?", "select", True, ["Yes", "No"])) == "Yes"
    office = Field("o", "Are you available to work from our office in Berlin?", "select", True, ["Yes, available for on-site work", "Yes, I’m willing to relocate to the office’s location and work on-site", "No, I’m only looking for a remote role"])
    assert _answer(office) == "No, I’m only looking for a remote role"
    bengaluru = Field("o", "Are you available to work from our office in Bengaluru?", "select", True, office.options)
    assert _answer(bengaluru, market="india", location="Bengaluru, India") == "Yes, I’m willing to relocate to the office’s location and work on-site"
    assert _answer(Field("p[]", "Please review our Privacy Notice before submitting your application.", "multiselect", True, ["Confirm"])) == ["Confirm"]


def test_how_heard_text_answer_and_twilio_careers_site():
    assert _answer(Field("h", "How did you hear about us?", "text", True, [])) == "LinkedIn"
    twilio = Field("h[]", "How did you hear about Twilio?", "multiselect", True, ["Careers Website", "LinkedIn", "Twitter", "Other"])
    assert _answer(twilio, "Twilio") == ["Careers Website"]


def test_office_question_as_text_follows_visa_status():
    office = Field("o", "Are you available to work from our office in Berlin?", "text", True, [])
    assert _answer(office) == "No"
    assert _answer(office, market="india", location="Bengaluru, India") == "Yes"


def test_post_employment_restrictions_and_open_source_links():
    restricted = Field("r", "Are you subject to any employment agreements and/or post-employment restrictions (e.g. non-compete)?", "select", True, ["Yes", "No"])
    assert _answer(restricted) == ("Yes" if PROFILE["declarations"]["restrictive_agreements"] else "No")
    links = Field("l", "Please share links of any open source projects you own or have made contributions to", "text", True, [])
    assert _answer(links) == PROFILE["links"]["github"]
