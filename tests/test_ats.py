import io
import json
import zipfile

import pytest

from agent.ats import (
    LEVELS, AtsInputError, blend, extract_text, parse_checks, record_check, recent_checks, score_resume,
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
