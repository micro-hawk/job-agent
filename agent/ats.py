import hashlib
import io
import json
import math
import re
import sqlite3
import zipfile
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import PyPdfError

LEVELS = {
    "fresher": ("Fresher (0–1 yrs)", 1),
    "junior": ("Junior (1–3 yrs)", 1),
    "mid": ("Mid (3–5 yrs)", 2),
    "senior": ("Senior (5–8 yrs)", 2),
    "lead": ("Lead (8+ yrs)", 3),
}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
WORDS_PER_PAGE = 500
MIN_WORDS = 150
NUMBERS_SHARE = 0.3
HISTORY = 20
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SECTIONS = {
    "Experience": r"experience|employment|work history",
    "Skills": r"skills|technologies|tech stack",
    "Education": r"education|academic",
}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")
YEAR = re.compile(r"\b(19|20)\d{2}\b")
SYSTEM = (
    "You are an applicant tracking system reviewer. Rate the resume below for the given experience level, judging only "
    "what the resume actually says. Give impact, seniority and clarity each as an integer from 0 to 100. impact: how well achievements show measurable results. seniority: how well the scope, "
    "ownership and leadership match the level. clarity: plain, scannable wording with strong action verbs. keywords: the "
    "most important skills and terms (up to 20, as short phrases) from the job description, or, when there is none, the "
    "ones typical for backend software roles at this level. fixes: the five most useful concrete changes. A fix may "
    "reword, reorder, quantify or surface what the resume already shows; never suggest adding experience, skills, "
    "employers or numbers the candidate has not shown."
)
RATING = {"type": "integer", "minimum": 0, "maximum": 100}
SCHEMA = {
    "type": "object",
    "properties": {
        "impact": RATING,
        "seniority": RATING,
        "clarity": RATING,
        "keywords": {"type": "array", "items": {"type": "string"}},
        "fixes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["impact", "seniority", "clarity", "keywords", "fixes"],
}


class AtsInputError(ValueError):
    pass


def extract_text(filename: str, data: bytes) -> tuple[str, int]:
    name = filename.lower()
    if name.endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join(page.extract_text() or "" for page in reader.pages), len(reader.pages)
        except (PyPdfError, ValueError, KeyError, TypeError) as exc:
            raise AtsInputError("could not read this PDF") from exc
    if name.endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                root = ElementTree.fromstring(archive.read("word/document.xml"))
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
            raise AtsInputError("could not read this DOCX") from exc
        text = "\n".join("".join(node.text or "" for node in paragraph.iter(f"{WORD_NS}t")) for paragraph in root.iter(f"{WORD_NS}p"))
        return text, max(1, math.ceil(len(text.split()) / WORDS_PER_PAGE))
    raise AtsInputError("upload a PDF or DOCX resume")


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def parse_checks(text: str, level: str, pages: int) -> list[dict]:
    words = len(text.split())
    missing = [section for section, pattern in SECTIONS.items() if not re.search(rf"^\W*(?:[A-Za-z&]+\s+){{0,2}}({pattern})\b[^\n]{{0,40}}$", text, re.I | re.M)]
    long_lines = [line for line in text.splitlines() if len(line.split()) >= 6]
    numbered = sum(1 for line in long_lines if re.search(r"\d", line))
    max_pages = LEVELS[level][1]
    return [
        _check("text", words >= MIN_WORDS, f"{words} words of selectable text" + ("" if words >= MIN_WORDS else ": scanned or image PDFs cannot be read by an ATS")),
        _check("sections", not missing, "Experience, Skills and Education headings found" if not missing else f"missing headings: {', '.join(missing)}"),
        _check("contact", bool(EMAIL.search(text) and PHONE.search(text)), "email and phone found" if EMAIL.search(text) and PHONE.search(text) else "add an email and phone number as plain text"),
        _check("length", pages <= max_pages, f"{pages} page{'' if pages == 1 else 's'} (aim for at most {max_pages} at this level)"),
        _check("numbers", bool(long_lines) and numbered / len(long_lines) >= NUMBERS_SHARE, f"{numbered} of {len(long_lines)} lines include a number or metric"),
        _check("dates", bool(YEAR.search(text)), "dates found" if YEAR.search(text) else "add start and end dates to each role"),
    ]


def blend(parse: int, content: int, keyword: int | None = None) -> int:
    if keyword is None:
        return round(0.3 * parse + 0.7 * content)
    return round(0.25 * parse + 0.35 * keyword + 0.4 * content)


def _clamp(value) -> int:
    return max(0, min(100, int(value or 0)))


def score_resume(text: str, pages: int, level: str, jd: str, llm, model: str) -> dict:
    if not text.strip():
        raise AtsInputError("the resume has no readable text; an ATS would see it as blank")
    checks = parse_checks(text, level, pages)
    parse = round(100 * sum(check["ok"] for check in checks) / len(checks))
    target = f"Job description:\n{jd.strip()}" if jd.strip() else "No job description given: judge against typical backend software roles at this level."
    prompt = f"Experience level: {LEVELS[level][0]}\n\n{target}\n\nResume:\n{text}"
    rated = llm.call("ats", model, SYSTEM, prompt, SCHEMA)
    keywords = list(dict.fromkeys(keyword.strip() for keyword in rated.get("keywords", []) if keyword.strip()))
    matched = [keyword for keyword in keywords if re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text, re.I)]
    keyword_pct = round(100 * len(matched) / len(keywords)) if keywords else 100
    ratings = [_clamp(rated.get(key)) for key in ("impact", "seniority", "clarity")]
    if max(ratings) <= 10:
        ratings = [rating * 10 for rating in ratings]
    content = round(sum(ratings) / 3) if jd.strip() else round((sum(ratings) + keyword_pct) / 4)
    keyword_score = keyword_pct if jd.strip() else None
    return {
        "score": blend(parse, content, keyword_score),
        "parse_score": parse,
        "content_score": content,
        "keyword_score": keyword_score,
        "ratings": dict(zip(("impact", "seniority", "clarity"), ratings)),
        "checks": checks,
        "matched_keywords": matched,
        "missing_keywords": [keyword for keyword in keywords if keyword not in matched],
        "fixes": [fix.strip() for fix in rated.get("fixes", []) if fix.strip()][:5],
        "with_jd": bool(jd.strip()),
    }


def fingerprint(data: bytes, level: str, jd: str) -> str:
    digest = hashlib.sha256(data)
    digest.update(b"\0" + level.encode("utf-8") + b"\0" + " ".join(jd.split()).encode("utf-8"))
    return digest.hexdigest()


def record_check(conn: sqlite3.Connection, filename: str, level: str, result: dict, now: str, fingerprint: str = "") -> int:
    cursor = conn.execute(
        "INSERT INTO ats_checks (filename, level, score, result, created_at, fingerprint) VALUES (?, ?, ?, ?, ?, ?)",
        (filename, level, result["score"], json.dumps(result), now, fingerprint),
    )
    conn.commit()
    return cursor.lastrowid


def delete_check(conn: sqlite3.Connection, check_id: int) -> bool:
    deleted = conn.execute("DELETE FROM ats_checks WHERE id = ?", (check_id,)).rowcount
    conn.commit()
    return bool(deleted)


def clear_checks(conn: sqlite3.Connection) -> int:
    deleted = conn.execute("DELETE FROM ats_checks").rowcount
    conn.commit()
    return deleted


def get_check(conn: sqlite3.Connection, check_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM ats_checks WHERE id = ?", (check_id,)).fetchone()
    return dict(row) | {"result": json.loads(row["result"]), "level_label": LEVELS.get(row["level"], (row["level"],))[0]} if row else None


def find_check(conn: sqlite3.Connection, fingerprint: str) -> dict | None:
    row = conn.execute("SELECT id FROM ats_checks WHERE fingerprint = ? AND fingerprint != '' ORDER BY id DESC LIMIT 1", (fingerprint,)).fetchone()
    return get_check(conn, row["id"]) if row else None


def recent_checks(conn: sqlite3.Connection, limit: int = HISTORY) -> list[dict]:
    return [
        dict(row) | {"result": json.loads(row["result"]), "level_label": LEVELS.get(row["level"], (row["level"],))[0]}
        for row in conn.execute("SELECT * FROM ats_checks ORDER BY id DESC LIMIT ?", (limit,))
    ]
