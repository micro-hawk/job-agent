import re
from datetime import date

from agent.apply.form import DEMOGRAPHIC_CONSENT, Field
from agent.llm import LLMError
from agent.profile import all_bullets, notice_answer, profile_brief

ANSWER_SYSTEM = (
    "You fill job application questions for the candidate described below. Answer only with facts stated in the "
    "candidate profile; never invent employers, numbers, projects or skills. Keep answers to one to three plain sentences, "
    "first person. If the profile does not contain enough facts to answer truthfully, return null."
)
ANSWER_SCHEMA = {"type": "object", "properties": {"answer": {"type": ["string", "null"]}}, "required": ["answer"]}
CHOOSE_SYSTEM = (
    "Pick the option that truthfully answers the job application question for the candidate described below. "
    "Return the option text exactly as listed, or null if no option is truthfully supported by the profile."
)
CHOOSE_SCHEMA = {"type": "object", "properties": {"option": {"type": ["string", "null"]}}, "required": ["option"]}
HEARD_PREFERENCES = (r"company.*(website|site|careers)", r"^(?!.*college).*careers? (site|page|website)", r"^linkedin$", r"job board", r"greenhouse", r"^other\b")
OFFICE_QUESTION = r"work from our office|work on-?site|office in "
DECLINE = re.compile(r"(don.?t|do not|decline|prefer not).*(answer|identify|disclose|say)|decline to self", re.I)


def _match(options: list[str], *patterns: str) -> str | None:
    for pattern in patterns:
        for option in options:
            if re.search(pattern, option, re.I):
                return option
    return None


def _yes_no(options: list[str], yes: bool) -> str | None:
    return _match(options, r"^yes\b" if yes else r"^no\b")


def _worked_here(company: str, resume: dict) -> bool:
    key = company.lower().split()[0]
    return any(key in entry["company"].lower() for entry in resume.get("experience", []))


def _eeo(field: Field, profile: dict) -> str | None:
    eeo = profile["eeo"]
    label = field.label.lower()
    if "hispanic" in label:
        choice = _match(field.options, r"^no\b", r"not hispanic")
    elif "gender" in label or label == "sex":
        choice = _match(field.options, rf"^{eeo['gender']}$", rf"^{eeo['gender']}\b")
    elif "race" in label or "ethnic" in label:
        choice = _match(field.options, r"^asian")
    elif "veteran" in label:
        choice = _match(field.options, r"not a protected veteran", r"^no\b")
    elif "disabilit" in label:
        choice = _match(field.options, r"no,? i (don.?t|do not) have", r"^no\b")
    else:
        return None
    return choice or _match(field.options, DECLINE.pattern)


def _heard(options: list[str], job: dict) -> str | None:
    company = re.escape(job["company"].split()[0])
    return _match(options, rf"^{company}\b.*careers?", *HEARD_PREFERENCES)


CITY_ALIASES = {"bengaluru": "bengaluru|bangalore", "bangalore": "bengaluru|bangalore", "gurugram": "gurugram|gurgaon", "mumbai": "mumbai|bombay"}
LAKH = 100_000


def _rupee_range(option: str) -> tuple[float, float] | None:
    numbers = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", option)]
    if not numbers:
        return None
    amounts = [n if n >= 1000 else n * LAKH for n in numbers]
    if re.search(r"below|under|less than|upto|up to", option, re.I):
        return 0, amounts[0]
    if len(amounts) == 1:
        return (amounts[0], float("inf")) if re.search(r"\+|above|more than|over", option, re.I) else None
    return amounts[0], amounts[1]


def _salary_option(options: list[str], lpa: float) -> str | None:
    amount = lpa * LAKH
    for option in options:
        bounds = _rupee_range(option)
        if bounds and bounds[0] <= amount <= bounds[1]:
            return option
    return None


def rule_answer(field: Field, job: dict, profile: dict, resume: dict, today: date):
    label = field.label.lower()
    links = profile["links"]
    india = job.get("market") == "india"
    if field.kind == "file":
        return profile["resume_pdf"] if field.name == "resume" else None
    if field.kind == "checkbox":
        return True if field.name == DEMOGRAPHIC_CONSENT else None
    if field.kind == "location":
        return profile["location"]["city"]
    simple = {
        "first_name": profile["first_name"], "last_name": profile["last_name"],
        "email": profile["email"], "phone": profile["phone"],
    }
    if field.name in simple:
        return simple[field.name]
    options = field.options
    job_city = re.escape((job.get("location") or "").split(",")[0].strip()) or "$^"
    if field.kind == "multiselect":
        if re.search(r"gender|race|ethnic|hispanic|veteran|disabilit|^sex$", label):
            choice = _eeo(field, profile)
        elif re.search(r"how did you (learn|hear|find)", label):
            choice = _heard(options, job)
        elif re.search(r"communicat", label):
            choice = _match(options, r"^e-?mail$")
        elif re.search(r"languages you speak|speak fluently", label):
            choice = _match(options, r"^english$")
        elif re.search(r"cities|office|locations?", label):
            choice = _match(options, rf"^{job_city}\b")
        elif re.search(r"select all|worked with|professional experience", label):
            skills = " ".join(item for group in resume.get("skills", []) for item in group["items"])
            known = [option for option in options if re.search(rf"(?<!\w){re.escape(option)}(?!\w)", skills, re.I)]
            return known or None
        else:
            choice = _match(options, r"acknowledge", r"\bagree\b", r"consent", r"\bconfirm\b")
        return [choice] if choice else None
    if field.kind == "select":
        if re.search(r"gender|race|ethnic|hispanic|veteran|disabilit|^sex$", label):
            return _eeo(field, profile)
        if re.search(r"sponsor|visa", label):
            return _yes_no(options, not india)
        if re.search(r"authori[sz]ed to work|eligible to work|right to work|legally work", label):
            return _yes_no(options, True if "you reside" in label else india)
        if re.search(r"(worked|been employed|employed) (at|by|for)|previously (worked|employed)", label):
            return _yes_no(options, _worked_here(job["company"], resume))
        if re.search(r"family|relative|personal relationship", label):
            return _yes_no(options, profile["declarations"]["relatives_at_company"])
        if re.search(r"non-?compete|non-?solicitation|agreements? with an? (current|former)|employment agreements|post-employment restrict", label):
            return _yes_no(options, profile["declarations"]["restrictive_agreements"])
        if re.search(r"outside business|side business|conflict of interest", label):
            return _yes_no(options, profile["declarations"]["outside_business_activities"])
        if re.search(r"stay up to date|alerts for similar|marketing|newsletter|subscribe", label):
            return _yes_no(options, False)
        if re.search(r"records? and transcribes|recording (of )?interviews", label):
            return _yes_no(options, profile["consent"])
        if re.search(OFFICE_QUESTION, label):
            if not india:
                return _yes_no(options, False)
            return (_match(options, r"^yes\b.*relocate") if profile["willing_to_relocate"] else None) or _yes_no(options, True)
        if re.search(r"relocat", label):
            return _yes_no(options, profile["willing_to_relocate"])
        if re.search(r"how did you (learn|hear|find)", label):
            return _heard(options, job)
        if re.search(r"contact your (current )?employer", label):
            return _yes_no(options, False)
        if re.search(r"preferred office|office location|location preference", label):
            return _match(options, rf"^{job_city}\b")
        if re.search(r"current (job )?location|currently (located|based)|where are you (currently )?(located|based)", label):
            city = profile["location"]["city"].lower()
            return _match(options, rf"^({CITY_ALIASES.get(city, re.escape(city))})\b")
        if re.search(r"current (ctc|salary|compensation)", label):
            return _salary_option(options, profile["current_ctc"]["total_lpa"])
        if re.search(r"\bagree\b|acknowledge|consent|privacy|confirm|review the linked", label):
            return _match(options, r"\bagree\b", r"acknowledge", r"\bconfirm\b", r"^yes\b")
        return None
    if re.search(r"how did you (learn|hear|find)", label):
        return "LinkedIn"
    if re.search(OFFICE_QUESTION, label):
        return "Yes" if india else "No"
    if re.search(r"preferred (first )?name", label):
        return profile["first_name"]
    if "linkedin" in label:
        return links["linkedin"]
    if "github" in label or re.search(r"open[- ]source", label):
        return links["github"]
    if re.search(r"website|portfolio", label):
        return links["portfolio"]
    if re.search(r"years of (professional |relevant |total )?experience (do you have )?(overall|in total|total)|total (years of )?experience", label):
        return str(profile["total_experience_years"])
    if re.search(r"notice period|when can you (start|join)|earliest (start|availability)", label):
        return notice_answer(today, profile)
    if re.search(r"salary expectation|expected (salary|ctc|compensation)|desired (salary|compensation)", label):
        return profile["expected_salary"].get(job.get("market"))
    if re.search(r"zip|postal code|pin ?code", label):
        return profile["location"]["postal_code"]
    if re.search(r"(home|street|mailing|residential) address|address line 1", label):
        return profile["location"]["address"]
    if re.search(r"^i certify", label):
        return f"{profile['name']}, {today.isoformat()}"
    if re.search(r"current (ctc|salary|compensation)", label):
        return profile["current_ctc"]["text"]
    city = profile["location"]["city"].lower()
    if re.search(r"based (out of|in)|currently located|reside in", label) and re.search(rf"{city}|bangalore", label):
        return "Yes"
    return None


def _llm_text(field: Field, job: dict, brief: str, llm) -> str | None:
    prompt = f"Job: {job['title']} at {job['company']}\n\nQuestion: {field.label}\n\nCandidate profile:\n{brief}"
    answer = llm.call("answer", "sonnet", ANSWER_SYSTEM, prompt, ANSWER_SCHEMA).get("answer")
    return answer.strip() if isinstance(answer, str) and answer.strip() else None


def _llm_choice(field: Field, job: dict, brief: str, llm) -> str | None:
    options = "\n".join(f"- {option}" for option in field.options)
    prompt = f"Job: {job['title']} at {job['company']}\n\nQuestion: {field.label}\nOptions:\n{options}\n\nCandidate profile:\n{brief}"
    option = llm.call("choose", "haiku", CHOOSE_SYSTEM, prompt, CHOOSE_SCHEMA).get("option")
    return option if option in field.options else None


def answer_brief(profile: dict, resume: dict) -> str:
    experience = "\n".join(
        f"- {entry['company']} ({entry.get('title', '')}): {bullet['text']}"
        for entry in resume.get("experience", []) for bullet in entry["bullets"]
    )
    projects = "\n".join(f"- {bullet['text']}" for entry in resume.get("projects", []) for bullet in entry["bullets"])
    return f"{profile_brief(profile, resume)}\nExperience:\n{experience}\nProjects:\n{projects}"


def answer_fields(fields: list[Field], job: dict, profile: dict, resume: dict, llm, today: date, prefilled: dict | None = None) -> tuple[dict, list[str]]:
    answers: dict = {}
    missing: list[str] = []
    brief = None
    for field in fields:
        value = rule_answer(field, job, profile, resume, today)
        if value is None and prefilled:
            value = prefilled.get(field.name)
        if value is None and field.required and field.kind in ("text", "textarea", "select"):
            brief = brief or answer_brief(profile, resume)
            try:
                value = _llm_choice(field, job, brief, llm) if field.kind == "select" else _llm_text(field, job, brief, llm)
            except LLMError:
                value = None
        if value is not None:
            answers[field.name] = value
        elif field.required:
            missing.append(field.label)
    return answers, missing
