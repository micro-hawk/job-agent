import json
from datetime import date

import pytest

from agent.questionnaire import (
    SENT_TEXT, answer_facts, collect_questionnaires, draft_answers, draft_pending, fill_and_submit, questionnaire_links,
    record_submit, suggest_pending,
)

from tests.fakes import NOW, StubLLM

PROFILE = {
    "total_experience_years": 4,
    "current_ctc": {"text": "₹16 LPA (₹15 LPA fixed + ₹1 LPA variable)"},
    "expected_salary": {"india": "₹28 LPA"},
    "notice": {"last_working_day": date(2026, 10, 30), "serving": "Serving notice, last working day 30 Oct 2026", "after": "Immediate joiner"},
    "location": {"city": "Bengaluru", "country": "India"},
    "willing_to_relocate": True,
}
TEXT_QUESTIONS = [
    {"id": 2, "text": "Notice Period", "type": 0, "required": True, "position": 1},
    {"id": 1, "text": "Which LLMs have you used?", "type": 0, "required": True, "position": 0},
    {"id": 3, "text": "Anything else?", "type": 1, "required": False, "position": 2},
]
CONV_API = "https://www.instahyre.com/api/v1/inbox_page/candidate_conversation?limit=10&offset={}"
MSG_API = "https://www.instahyre.com/api/v1/resume_modal/emails/message?conv_id={}"
Q_API = "https://www.instahyre.com/api/v1/questionnaires/candidate_questionnaire/{}"


def message(html):
    return {"content_html": html}


def test_questionnaire_links_are_found_once_per_message_set():
    messages = [
        message('<a href="https://www.instahyre.com/questionnaire/116649/6259321039">Click here to open questionnaire</a>'),
        message('again <a href="/questionnaire/116649/6259321039">link</a> and <a href="/questionnaire/7/8">other</a>'),
        message("no link"),
    ]
    assert questionnaire_links(messages) == [("116649", "6259321039"), ("7", "8")]


def fake_site(conversations, messages, questionnaires, sent):
    pages = {CONV_API.format(0): {"objects": conversations}, CONV_API.format(10): {"objects": []}}
    for conv_id, html in messages.items():
        pages[MSG_API.format(conv_id)] = {"objects": [message(html)]}
    for qid, questions in questionnaires.items():
        pages[Q_API.format(qid)] = {"id": int(qid), "questions": questions}
    requested = []

    def get_json(url):
        requested.append(url)
        return pages[url]

    def page_text(url):
        requested.append(url)
        return SENT_TEXT if url in sent else "Questionnaire for Acme"

    return get_json, page_text, requested


def rows(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM questionnaires ORDER BY id")]


def test_collect_stores_new_questionnaires_with_their_state(conn):
    conversations = [{"id": 10, "job_title": "Acme - SDE 2"}, {"id": 11, "job_title": "Beta - Java Engineer"}, {"id": 12, "job_title": "Gamma - SDE"}]
    messages = {10: '<a href="/questionnaire/1/100">q</a>', 11: '<a href="/questionnaire/2/200">q</a>', 12: '<a href="/questionnaire/3/300">q</a>'}
    questionnaires = {"1": TEXT_QUESTIONS, "3": [{"id": 9, "text": "Rate your Java", "type": 4, "required": True, "position": 0}]}
    get_json, page_text, _ = fake_site(conversations, messages, questionnaires, {"https://www.instahyre.com/questionnaire/2/200"})
    stats = collect_questionnaires(get_json, page_text, conn, NOW)
    assert stats == {"questionnaires_new": 3, "to_review": 1, "already_sent": 1, "open_yourself": 1}
    stored = rows(conn)
    assert [(row["job_title"], row["status"]) for row in stored] == [("Acme - SDE 2", "draft"), ("Beta - Java Engineer", "sent"), ("Gamma - SDE", "manual")]
    assert stored[0]["url"] == "https://www.instahyre.com/questionnaire/1/100"
    assert [question["id"] for question in json.loads(stored[0]["questions"])] == [1, 2, 3]


def test_collect_skips_questionnaires_it_already_knows(conn):
    conversations = [{"id": 10, "job_title": "Acme - SDE 2"}]
    get_json, page_text, requested = fake_site(conversations, {10: '<a href="/questionnaire/1/100">q</a>'}, {"1": TEXT_QUESTIONS}, set())
    collect_questionnaires(get_json, page_text, conn, NOW)
    requested.clear()
    assert collect_questionnaires(get_json, page_text, conn, NOW)["questionnaires_new"] == 0
    assert Q_API.format(1) not in requested and len(rows(conn)) == 1


def test_answer_facts_state_salary_notice_and_relocation():
    facts = answer_facts(PROFILE, date(2026, 10, 5))
    assert "4 years" in facts and "₹16 LPA" in facts and "₹28 LPA" in facts
    assert "Serving notice, last working day 30 Oct 2026" in facts and "relocate" in facts


def test_draft_answers_keeps_unsupported_questions_blank():
    llm = StubLLM({"questionnaire": lambda prompt: {"answers": [
        {"id": 1, "answer": "  I integrated OpenAI GPT-4 into a support bot.  "},
        {"id": 2, "answer": None},
        {"id": 99, "answer": "invented"},
    ]}})
    answers = draft_answers(TEXT_QUESTIONS, "Acme - SDE 2", "brief", "facts", llm, "sonnet")
    assert answers == {"1": "I integrated OpenAI GPT-4 into a support bot.", "2": "", "3": ""}
    task, model, prompt = llm.calls[0]
    assert (task, model) == ("questionnaire", "sonnet")
    assert "[1] Which LLMs have you used?" in prompt and "brief" in prompt and "facts" in prompt


def test_draft_pending_fills_only_drafts_without_answers(conn):
    conn.execute("INSERT INTO questionnaires (questionnaire_id, opportunity_id, job_title, url, questions, status, created_at) VALUES ('1', '100', 'Acme', 'u', ?, 'draft', ?)", (json.dumps(TEXT_QUESTIONS), NOW))
    conn.execute("INSERT INTO questionnaires (questionnaire_id, opportunity_id, job_title, url, questions, status, created_at) VALUES ('2', '200', 'Beta', 'u', '[]', 'sent', ?)", (NOW,))
    llm = StubLLM({"questionnaire": lambda prompt: {"answers": [{"id": 1, "answer": "GPT-4"}]}})
    assert draft_pending(conn, llm, "sonnet", "brief", "facts") == 1
    assert draft_pending(conn, llm, "sonnet", "brief", "facts") == 0
    assert json.loads(rows(conn)[0]["answers"]) == {"1": "GPT-4", "2": "", "3": ""}


class FakeInputs:
    def __init__(self, page):
        self.page = page

    def count(self):
        return self.page.input_count

    def nth(self, index):
        page = self.page

        class Input:
            def fill(self, value):
                page.filled.append((index, value))
        return Input()


class FakeSubmit:
    def __init__(self, page):
        self.page = page

    def click(self):
        self.page.submitted = True
        if self.page.confirms:
            self.page.text = SENT_TEXT


class FakeQuestionnairePage:
    def __init__(self, text, input_count=3, confirms=True):
        self.text, self.input_count, self.confirms = text, input_count, confirms
        self.url = "https://www.instahyre.com/questionnaire/1/100"
        self.filled, self.submitted = [], False

    def goto(self, url, wait_until=None):
        self.visited = url

    def wait_for_timeout(self, ms):
        pass

    def inner_text(self, selector):
        return self.text

    def locator(self, selector):
        return FakeSubmit(self) if "submitQuestionnaire" in selector else FakeInputs(self)


ON_PAGE = "Questionnaire for Acme\nWhich LLMs have you used? REQUIRED\nNotice Period REQUIRED\nAnything else?\nSubmit"
ANSWERS = {"1": "GPT-4", "2": "Immediate joiner", "3": ""}
ORDERED = sorted(TEXT_QUESTIONS, key=lambda question: question["position"])


def test_fill_and_submit_fills_in_page_order_and_confirms():
    page = FakeQuestionnairePage(ON_PAGE)
    assert fill_and_submit(page, page.url, ORDERED, ANSWERS) == "submitted"
    assert page.filled == [(0, "GPT-4"), (1, "Immediate joiner"), (2, "")] and page.submitted


def test_fill_and_submit_refuses_blank_required_answers_without_opening_the_page():
    page = FakeQuestionnairePage(ON_PAGE)
    assert fill_and_submit(page, page.url, ORDERED, ANSWERS | {"2": "  "}) == "blank"
    assert not hasattr(page, "visited")


def test_fill_and_submit_stops_when_the_form_does_not_match():
    assert fill_and_submit(FakeQuestionnairePage(ON_PAGE, input_count=2), "u", ORDERED, ANSWERS) == "form_changed"
    reordered = FakeQuestionnairePage("Notice Period\nWhich LLMs have you used?\nAnything else?")
    assert fill_and_submit(reordered, "u", ORDERED, ANSWERS) == "form_changed" and not reordered.filled


def test_fill_and_submit_reports_already_sent_and_unconfirmed():
    assert fill_and_submit(FakeQuestionnairePage(SENT_TEXT), "u", ORDERED, ANSWERS) == "already"
    page = FakeQuestionnairePage(ON_PAGE, confirms=False)
    assert fill_and_submit(page, "u", ORDERED, ANSWERS) == "unconfirmed" and page.submitted


def test_fill_and_submit_stops_on_a_challenge():
    from agent.discover.instahyre import InstahyreBlocked
    with pytest.raises(InstahyreBlocked):
        fill_and_submit(FakeQuestionnairePage("Performing security verification"), "u", ORDERED, ANSWERS)


def test_record_submit_marks_the_outcome(conn):
    conn.execute("INSERT INTO questionnaires (questionnaire_id, opportunity_id, job_title, url, status, created_at) VALUES ('1', '100', 'Acme', 'u', 'draft', ?)", (NOW,))
    record_submit(conn, 1, "submitted", NOW)
    assert (rows(conn)[0]["status"], rows(conn)[0]["submitted_at"]) == ("submitted", NOW)
    conn.execute("UPDATE questionnaires SET status='draft'")
    record_submit(conn, 1, "unconfirmed", NOW)
    assert rows(conn)[0]["status"] == "draft" and "did not confirm" in rows(conn)[0]["reason"]


def test_drafting_prompt_keeps_answers_in_the_candidates_voice():
    from agent.questionnaire import SYSTEM
    assert "never mention the profile" in SYSTEM.lower()


def test_suggest_pending_suggests_only_for_blank_answers(conn):
    conn.execute(
        "INSERT INTO questionnaires (questionnaire_id, opportunity_id, job_title, url, questions, answers, status, created_at) VALUES ('1', '100', 'Acme', 'u', ?, ?, 'draft', ?)",
        (json.dumps(TEXT_QUESTIONS), json.dumps({"1": "GPT-4", "2": "", "3": ""}), NOW),
    )
    conn.execute("INSERT INTO questionnaires (questionnaire_id, opportunity_id, job_title, url, questions, answers, status, created_at) VALUES ('2', '200', 'Beta', 'u', ?, ?, 'draft', ?)", (json.dumps(TEXT_QUESTIONS), json.dumps({"1": "a", "2": "b", "3": "c"}), NOW))
    llm = StubLLM({"questionnaire_suggest": lambda prompt: {"answers": [{"id": 2, "answer": " I can join after 30 Oct. "}, {"id": 1, "answer": "x"}, {"id": 3, "answer": None}]}})
    assert suggest_pending(conn, llm, "sonnet", "brief", "facts") == 1
    assert suggest_pending(conn, llm, "sonnet", "brief", "facts") == 0
    assert json.loads(rows(conn)[0]["suggestions"]) == {"2": "I can join after 30 Oct."}
    task, _, prompt = llm.calls[0]
    assert task == "questionnaire_suggest" and "[2] Notice Period" in prompt and "[1] Which LLMs" not in prompt


def test_suggestion_prompt_forbids_invented_experience():
    from agent.questionnaire import SUGGEST_SYSTEM
    assert "never claim" in SUGGEST_SYSTEM.lower() and "have not" in SUGGEST_SYSTEM.lower()


def test_existing_databases_gain_the_suggestions_column(tmp_path):
    import sqlite3
    from agent.db import connect
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE questionnaires (id INTEGER PRIMARY KEY, questionnaire_id TEXT NOT NULL, opportunity_id TEXT NOT NULL, job_title TEXT NOT NULL, url TEXT NOT NULL, questions TEXT NOT NULL DEFAULT '[]', answers TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, submitted_at TEXT, UNIQUE (questionnaire_id, opportunity_id))")
    old.commit()
    old.close()
    columns = [row["name"] for row in connect(db).execute("PRAGMA table_info(questionnaires)")]
    assert "suggestions" in columns
