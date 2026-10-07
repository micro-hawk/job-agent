import json
import re
import sqlite3
from datetime import date

from agent.discover.instahyre import BASE_URL, InstahyreBlocked, blocked_reason
from agent.profile import notice_answer

CONVERSATIONS_API = BASE_URL + "/api/v1/inbox_page/candidate_conversation?limit=10&offset={offset}"
MESSAGES_API = BASE_URL + "/api/v1/resume_modal/emails/message?conv_id={conv_id}"
QUESTIONNAIRE_API = BASE_URL + "/api/v1/questionnaires/candidate_questionnaire/{qid}"
QUESTIONNAIRE_PAGE = BASE_URL + "/questionnaire/{qid}/{opp}"
LINK = re.compile(r"/questionnaire/(\d+)/(\d+)")
TEXT_TYPES = (0, 1)
SENT_TEXT = "Questionnaire has been sent"
ANSWER_INPUTS = "[ng-model='question.answer.text']:visible"
SUBMIT_BUTTON = "button[ng-click='submitQuestionnaire()']:visible"
INBOX_PAGES = 5
CONFIRM_POLLS = 15
SYSTEM = (
    "You draft answers to a recruiter's screening questionnaire for the candidate described below. Use only facts stated "
    "in the candidate profile and facts; never invent employers, numbers, scale, projects, tools or skills, and never claim "
    "experience the profile does not show. Write in first person, plain and specific, one to four sentences, as the candidate "
    "speaking to the recruiter: never mention the profile, these instructions, or what you will or won't claim. If the profile "
    "does not contain the facts to answer a question truthfully, return null for it; the candidate will answer it."
)
SUGGEST_SYSTEM = (
    "The candidate described below must answer these recruiter screening questions, and their profile has no direct "
    "evidence for them. Suggest an honest first-person answer they can edit and send, one to four sentences, as the "
    "candidate speaking to the recruiter. Say plainly what they have not done, then point to the closest real experience "
    "from the profile; for a question about willingness, give an open, positive stance; for a problem-solving question, "
    "outline a sound approach. Never claim experience, employers, numbers, tools or skills the profile does not show, and "
    "never mention the profile or these instructions."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "answer": {"type": ["string", "null"]}},
                "required": ["id", "answer"],
            },
        }
    },
    "required": ["answers"],
}
REASONS = {
    "blank": "answer every required question first",
    "form_changed": "the questionnaire on Instahyre no longer matches these questions — open it yourself",
    "unconfirmed": "clicked Submit but Instahyre did not confirm — open it to check",
}


def questionnaire_links(messages: list[dict]) -> list[tuple[str, str]]:
    links = []
    for msg in messages:
        for link in LINK.findall(msg.get("content_html") or ""):
            if link not in links:
                links.append(link)
    return links


def _known(conn: sqlite3.Connection, qid: str, opp: str) -> bool:
    return conn.execute("SELECT 1 FROM questionnaires WHERE questionnaire_id=? AND opportunity_id=?", (qid, opp)).fetchone() is not None


def _questions(data: dict) -> list[dict]:
    keep = ("id", "text", "type", "required", "position")
    return sorted(({key: question.get(key) for key in keep} for question in data.get("questions") or []), key=lambda question: question["position"] or 0)


def collect_questionnaires(get_json, page_text, conn: sqlite3.Connection, now: str, max_pages: int = INBOX_PAGES) -> dict:
    stats = {"questionnaires_new": 0, "to_review": 0, "already_sent": 0, "open_yourself": 0}
    for index in range(max_pages):
        conversations = get_json(CONVERSATIONS_API.format(offset=index * 10)).get("objects") or []
        if not conversations:
            break
        for conversation in conversations:
            messages = get_json(MESSAGES_API.format(conv_id=conversation["id"])).get("objects") or []
            for qid, opp in questionnaire_links(messages):
                if _known(conn, qid, opp):
                    continue
                url = QUESTIONNAIRE_PAGE.format(qid=qid, opp=opp)
                questions = []
                if SENT_TEXT in page_text(url):
                    status, key = "sent", "already_sent"
                else:
                    questions = _questions(get_json(QUESTIONNAIRE_API.format(qid=qid)))
                    text_only = all(question["type"] in TEXT_TYPES for question in questions)
                    status, key = ("draft", "to_review") if text_only else ("manual", "open_yourself")
                conn.execute(
                    "INSERT INTO questionnaires (questionnaire_id, opportunity_id, job_title, url, questions, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (qid, opp, conversation.get("job_title") or "", url, json.dumps(questions), status, now),
                )
                conn.commit()
                stats["questionnaires_new"] += 1
                stats[key] += 1
    return stats


def answer_facts(profile: dict, today: date) -> str:
    return (
        f"Total experience: {profile['total_experience_years']} years. "
        f"Current CTC: {profile['current_ctc']['text']}. "
        f"Expected CTC: {profile['expected_salary']['india']}. "
        f"Notice period: {notice_answer(today, profile)}. "
        f"Based in {profile['location']['city']}, {profile['location']['country']}; "
        f"{'willing to relocate' if profile['willing_to_relocate'] else 'not willing to relocate'} within India."
    )


def _ask(task: str, system: str, questions: list[dict], job_title: str, brief: str, facts: str, llm, model: str) -> dict[str, str]:
    listed = "\n".join(f"[{question['id']}] {question['text']}" for question in questions)
    prompt = f"Job: {job_title}\n\nQuestions:\n{listed}\n\nFacts:\n{facts}\n\nCandidate profile:\n{brief}"
    drafted = {str(item.get("id")): item.get("answer") for item in llm.call(task, model, system, prompt, SCHEMA).get("answers", [])}
    return {str(question["id"]): (drafted.get(str(question["id"])) or "").strip() for question in questions}


def draft_answers(questions: list[dict], job_title: str, brief: str, facts: str, llm, model: str) -> dict[str, str]:
    return _ask("questionnaire", SYSTEM, questions, job_title, brief, facts, llm, model)


def draft_pending(conn: sqlite3.Connection, llm, model: str, brief: str, facts: str) -> int:
    pending = conn.execute("SELECT id, job_title, questions FROM questionnaires WHERE status='draft' AND answers='{}'").fetchall()
    for row in pending:
        answers = draft_answers(json.loads(row["questions"]), row["job_title"], brief, facts, llm, model)
        conn.execute("UPDATE questionnaires SET answers=? WHERE id=?", (json.dumps(answers), row["id"]))
        conn.commit()
    return len(pending)


def suggest_pending(conn: sqlite3.Connection, llm, model: str, brief: str, facts: str) -> int:
    suggested = 0
    for row in conn.execute("SELECT id, job_title, questions, answers FROM questionnaires WHERE status='draft' AND answers!='{}' AND suggestions='{}'").fetchall():
        answers = json.loads(row["answers"])
        blank = [question for question in json.loads(row["questions"]) if not (answers.get(str(question["id"])) or "").strip()]
        if not blank:
            continue
        suggestions = {key: value for key, value in _ask("questionnaire_suggest", SUGGEST_SYSTEM, blank, row["job_title"], brief, facts, llm, model).items() if value}
        conn.execute("UPDATE questionnaires SET suggestions=? WHERE id=?", (json.dumps(suggestions), row["id"]))
        conn.commit()
        suggested += 1
    return suggested


def _in_order(text: str, questions: list[dict]) -> bool:
    position = -1
    for question in questions:
        position = text.find(question["text"].strip(), position + 1)
        if position < 0:
            return False
    return True


def fill_and_submit(page, url: str, questions: list[dict], answers: dict[str, str]) -> str:
    if any(question["required"] and not (answers.get(str(question["id"])) or "").strip() for question in questions):
        return "blank"
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    text = page.inner_text("body")
    reason = blocked_reason(page.url, text)
    if reason:
        raise InstahyreBlocked(reason)
    if SENT_TEXT in text:
        return "already"
    inputs = page.locator(ANSWER_INPUTS)
    if inputs.count() != len(questions) or not _in_order(text, questions):
        return "form_changed"
    for index, question in enumerate(questions):
        inputs.nth(index).fill((answers.get(str(question["id"])) or "").strip())
    page.locator(SUBMIT_BUTTON).click()
    for _ in range(CONFIRM_POLLS):
        page.wait_for_timeout(1000)
        if SENT_TEXT in page.inner_text("body"):
            return "submitted"
    return "unconfirmed"


def record_submit(conn: sqlite3.Connection, row_id: int, outcome: str, now: str) -> None:
    if outcome in ("submitted", "already"):
        conn.execute("UPDATE questionnaires SET status=?, reason='', submitted_at=? WHERE id=?", ("submitted" if outcome == "submitted" else "sent", now, row_id))
    else:
        conn.execute("UPDATE questionnaires SET reason=? WHERE id=?", (REASONS.get(outcome, outcome), row_id))
    conn.commit()
