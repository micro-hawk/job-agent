import io
import json
import zipfile

import pytest

from agent.ats import (
    LEVELS, SCHEMA, SYSTEM, AtsInputError, blend, clear_checks, delete_check, extract_text, find_check, fingerprint, parse_checks, record_check, recent_checks,
    score_resume,
)

from tests.fakes import NOW, StubLLM

RESUME = """Jane Doe
jane@example.com | +91 98765 43210 | Bengaluru

EXPERIENCE
Software Engineer, Acme — Jan 2022 – Present
- Built Kafka event pipeline processing 5M records per day
- Cut API latency by 30% with Redis caching
- Owned the attendance module serving 700+ clients
- Mentored 3 engineers through code reviews and design sessions

SKILLS
Java, Spring Boot, Kafka, Redis, PostgreSQL, Docker

EDUCATION
B.Tech Computer Science, 2021
""" + "\n".join(f"- Worked on backend service number {index} with the platform team every sprint" for index in range(12))


def make_pdf(pages: list[list[str]]) -> bytes:
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        text = " ".join(f"({line.replace('(', '').replace(')', '')}) Tj T*" for line in lines)
        stream = f"BT /F1 10 Tf 12 TL 40 800 Td {text} ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text.replace('&', '&amp;').replace('<', '&lt;')}</w:t></w:r></w:p>" for text in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    return buffer.getvalue()


def test_extract_text_reads_pdf_pages():
    text, pages = extract_text("cv.pdf", make_pdf([["Jane Doe", "EXPERIENCE"], ["SKILLS Java"]]))
    assert "Jane Doe" in text and "SKILLS Java" in text and pages == 2


def test_extract_text_reads_docx_and_estimates_pages():
    text, pages = extract_text("CV.DOCX", make_docx(["Jane Doe", "R&D <team>"] + ["word " * 100] * 6))
    assert "Jane Doe\nR&D <team>" in text and pages == 2


def test_extract_text_rejects_other_or_broken_files():
    with pytest.raises(AtsInputError, match="PDF or DOCX"):
        extract_text("cv.txt", b"hello")
    with pytest.raises(AtsInputError, match="could not read"):
        extract_text("cv.pdf", b"not a pdf")
    with pytest.raises(AtsInputError, match="could not read"):
        extract_text("cv.docx", b"not a zip")


def checks_by_name(text, level, pages=1):
    return {check["name"]: check for check in parse_checks(text, level, pages)}


def test_parse_checks_pass_for_a_well_formed_resume():
    checks = checks_by_name(RESUME, "mid")
    assert all(check["ok"] for check in checks.values()), [check for check in checks.values() if not check["ok"]]
    assert set(checks) == {"text", "sections", "contact", "length", "numbers", "dates"}


def test_parse_checks_flag_what_an_ats_would_miss():
    checks = checks_by_name("Jane\nSome words only", "fresher", pages=3)
    assert not checks["text"]["ok"] and not checks["contact"]["ok"] and not checks["dates"]["ok"]
    assert "Experience" in checks["sections"]["detail"] and "3 pages" in checks["length"]["detail"]
    assert checks_by_name(RESUME, "lead", pages=3)["length"]["ok"] and not checks_by_name(RESUME, "junior", pages=2)["length"]["ok"]


def test_blend_weights_parse_content_and_keywords():
    assert blend(100, 50) == 65
    assert blend(100, 50, 80) == 73
    assert set(LEVELS) == {"fresher", "junior", "mid", "senior", "lead"}


def rubric(keywords, impact=80, seniority=60, clarity=70):
    return lambda prompt: {"impact": impact, "seniority": seniority, "clarity": clarity, "keywords": keywords, "fixes": ["Add a summary", " Quantify the Kafka work "]}


def test_score_resume_matches_jd_keywords_against_the_resume_text():
    llm = StubLLM({"ats": rubric(["Kafka", "Spring Boot", "Golang", "Kubernetes", "redis"])})
    result = score_resume(RESUME, 1, "mid", "We need Kafka, Golang and Kubernetes", llm, "sonnet")
    assert result["matched_keywords"] == ["Kafka", "Spring Boot", "redis"] and result["missing_keywords"] == ["Golang", "Kubernetes"]
    assert result["keyword_score"] == 60 and result["content_score"] == 70 and result["parse_score"] == 100
    assert result["score"] == blend(100, 70, 60) and result["fixes"] == ["Add a summary", "Quantify the Kafka work"]
    task, model, prompt = llm.calls[0]
    assert (task, model) == ("ats", "sonnet") and "Mid (3–5 yrs)" in prompt and "We need Kafka" in prompt and "Jane Doe" in prompt


def test_score_resume_without_jd_folds_typical_keywords_into_content():
    llm = StubLLM({"ats": rubric(["Kafka", "Golang"])})
    result = score_resume(RESUME, 1, "senior", "", llm, "sonnet")
    assert result["keyword_score"] is None and result["content_score"] == round((80 + 60 + 70 + 50) / 4)
    assert result["score"] == blend(100, result["content_score"]) and "no job description" in llm.calls[0][2].lower()


def test_ratings_are_asked_for_on_a_0_to_100_scale():
    assert "0 to 100" in SYSTEM
    assert all(SCHEMA["properties"][key] == {"type": "integer", "minimum": 0, "maximum": 100} for key in ("impact", "seniority", "clarity"))


def test_ratings_given_out_of_10_are_scaled_to_100():
    llm = StubLLM({"ats": rubric(["Kafka"], impact=8, seniority=7, clarity=8)})
    result = score_resume(RESUME, 1, "mid", "", llm, "sonnet")
    assert result["ratings"] == {"impact": 80, "seniority": 70, "clarity": 80}


def test_low_ratings_on_the_100_scale_are_kept_when_one_is_above_10():
    llm = StubLLM({"ats": rubric(["Kafka"], impact=8, seniority=40, clarity=9)})
    assert score_resume(RESUME, 1, "mid", "", llm, "sonnet")["ratings"] == {"impact": 8, "seniority": 40, "clarity": 9}


def test_keywords_match_across_line_breaks():
    llm = StubLLM({"ats": rubric(["System Design"])})
    result = score_resume(RESUME + "\nSkills: Microservices, System\nDesign", 1, "mid", "", llm, "sonnet")
    assert result["matched_keywords"] == ["System Design"]


def test_score_resume_rejects_resumes_without_text():
    with pytest.raises(AtsInputError, match="no readable text"):
        score_resume("   ", 1, "mid", "", StubLLM({}), "sonnet")


def test_checks_are_kept_as_history(conn):
    llm = StubLLM({"ats": rubric(["Kafka"])})
    for name in ("a.pdf", "b.pdf"):
        record_check(conn, name, "mid", score_resume(RESUME, 1, "mid", "", llm, "sonnet"), NOW)
    history = recent_checks(conn)
    assert [check["filename"] for check in history] == ["b.pdf", "a.pdf"]
    assert history[0]["result"]["parse_score"] == 100 and history[0]["level_label"] == "Mid (3–5 yrs)"
    assert "Jane Doe" not in json.dumps(history)


def test_section_headings_may_have_a_qualifier():
    text = RESUME.replace("EXPERIENCE", "Professional Experience").replace("SKILLS", "Technical Skills")
    assert checks_by_name(text, "mid")["sections"]["ok"]
    prose = RESUME.replace("SKILLS\n", "I picked up many new skills while working with the platform team\n")
    assert "Skills" in checks_by_name(prose, "mid")["sections"]["detail"]


def test_checks_can_be_deleted_one_at_a_time_or_all(conn):
    llm = StubLLM({"ats": rubric(["Kafka"])})
    ids = [record_check(conn, name, "mid", score_resume(RESUME, 1, "mid", "", llm, "sonnet"), NOW) for name in ("a.pdf", "b.pdf", "c.pdf")]
    assert delete_check(conn, ids[1]) and not delete_check(conn, ids[1])
    assert [check["filename"] for check in recent_checks(conn)] == ["c.pdf", "a.pdf"]
    assert clear_checks(conn) == 2 and recent_checks(conn) == []


def test_fingerprint_covers_file_level_and_job_description_but_not_whitespace():
    base = fingerprint(b"resume", "mid", "Kafka  and\nGolang ")
    assert base == fingerprint(b"resume", "mid", "Kafka and Golang") and len(base) == 64
    assert len({base, fingerprint(b"resume2", "mid", "Kafka and Golang"), fingerprint(b"resume", "senior", "Kafka and Golang"), fingerprint(b"resume", "mid", "")}) == 4


def test_saved_checks_are_found_by_fingerprint(conn):
    llm = StubLLM({"ats": rubric(["Kafka"])})
    result = score_resume(RESUME, 1, "mid", "", llm, "sonnet")
    older = record_check(conn, "cv.pdf", "mid", result, NOW, fingerprint="abc")
    newer = record_check(conn, "cv-renamed.pdf", "mid", result, NOW, fingerprint="abc")
    record_check(conn, "other.pdf", "mid", result, NOW)
    assert find_check(conn, "abc")["id"] == newer and find_check(conn, "abc")["result"] == result
    assert find_check(conn, "zzz") is None and older != newer


def test_old_databases_gain_the_fingerprint_column(tmp_path):
    import sqlite3
    from agent.db import connect
    db = tmp_path / "old.db"
    sqlite3.connect(db).executescript("CREATE TABLE ats_checks (id INTEGER PRIMARY KEY, filename TEXT NOT NULL, level TEXT NOT NULL, score INTEGER NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);")
    assert "fingerprint" in {row["name"] for row in connect(db).execute("PRAGMA table_info(ats_checks)")}
