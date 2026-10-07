import threading
from datetime import date

import httpx

from agent.ats import record_check
from agent.db import connect
from agent.resume_builder import CompileError, create_draft, get_draft
from dashboard.server import make_server, render

from tests.fakes import NOW, StubLLM
from tests.test_dashboard import ats_result
from tests.test_resume_builder import PROFILE, RESUME

TODAY = date(2026, 10, 7)


class FakeCompiler:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def __call__(self, tex):
        self.calls.append(tex)
        if self.error:
            raise self.error
        return b"%PDF-1.4 fake"


def serve(db, llm=None, compiler=None, loader=lambda: (RESUME, PROFILE)):
    server = make_server(db, "127.0.0.1", 0, llm_factory=lambda conn: llm, resume_loader=loader, compiler=compiler or FakeCompiler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def stop(server):
    server.shutdown()
    server.server_close()


def test_start_screen_lists_templates_sources_and_drafts(conn):
    check = record_check(conn, "cv.pdf", "mid", ats_result(fixes=["Quantify the Kafka work"]), NOW)
    record_check(conn, "nofix.pdf", "mid", ats_result(fixes=[]), NOW)
    create_draft(conn, "Classic · master resume", "classic", "master", NOW, tex="x")
    html = render(conn, "resume", TODAY, builder={"check": str(check)})
    assert 'href="/?view=resume" class="nav-item on"' in html and 'action="/resume/new"' in html
    assert html.count('name="template"') == 3 and "Jake Gutierrez" in html and 'value="master"' in html
    assert f'value="{check}" checked' in html and "cv.pdf" in html and "nofix.pdf" not in html
    assert "Classic · master resume" in html and "Quantify the Kafka work" in html


def test_ats_result_links_to_the_builder(conn):
    check = record_check(conn, "cv.pdf", "mid", ats_result(), NOW)
    assert f'href="/?view=resume&check={check}"' in render(conn, "ats", TODAY)


def test_master_resume_draft_compiles_and_downloads(tmp_path):
    db = tmp_path / "agent.db"
    conn = connect(db)
    compiler = FakeCompiler()
    server, base = serve(db, compiler=compiler)
    try:
        response = httpx.post(f"{base}/resume/new", data={"template": "classic", "source": "master"})
        assert response.status_code == 303 and response.headers["location"].startswith("/?view=resume&draft=")
        draft_id = int(response.headers["location"].rsplit("=", 1)[1])
        page = httpx.get(f"{base}{response.headers['location']}").text
        pdf = httpx.get(f"{base}/resume/{draft_id}.pdf")
        tex = httpx.get(f"{base}/resume/{draft_id}.tex")
    finally:
        stop(server)
    assert "Northwind Payments" in compiler.calls[0] and 'name="tex"' in page and "https://www.overleaf.com/docs" in page
    assert pdf.content == b"%PDF-1.4 fake" and pdf.headers["content-type"] == "application/pdf"
    assert "Alex Morgan" in tex.text and "attachment" in tex.headers["content-disposition"]
    assert (tmp_path / "resumes" / f"{draft_id}.pdf").exists()


def test_fixes_flow_shows_proposals_and_applies_only_ticked_ones(tmp_path):
    db = tmp_path / "agent.db"
    conn = connect(db)
    check = record_check(conn, "cv.pdf", "mid", ats_result(fixes=["Tighten wording"]), NOW)
    llm = StubLLM({"resume_rewrite": lambda prompt: {"changes": [
        {"target": "headline", "after": "Backend Engineer for payments platforms", "fix": "Tighten wording"},
        {"target": "nw-search", "after": "Removed deep-page timeouts in transaction search with cursor pagination.", "fix": "Tighten wording"},
    ]}})
    server, base = serve(db, llm=llm)
    try:
        location = httpx.post(f"{base}/resume/new", data={"template": "modern", "source": str(check)}).headers["location"]
        draft_id = int(location.rsplit("=", 1)[1])
        review = httpx.get(f"{base}{location}").text
        response = httpx.post(f"{base}/resume/{draft_id}/generate", data={"accept": ["nw-search"]})
        editor = httpx.get(f"{base}{location}").text
    finally:
        stop(server)
    assert "Tighten wording" in llm.calls[0][2]
    assert "Backend Engineer for payments platforms" in review and "Senior Backend Engineer" in review and 'name="accept"' in review
    assert response.headers["location"] == location and 'name="tex"' in editor
    tex = get_draft(conn, draft_id)["tex"]
    assert "Removed deep-page timeouts" in tex and "Senior Backend Engineer" in tex and "payments platforms" not in tex


def test_saving_returns_compile_errors_as_json(tmp_path):
    db = tmp_path / "agent.db"
    conn = connect(db)
    draft_id = create_draft(conn, "Draft", "classic", "master", NOW, tex="old")
    server, base = serve(db, compiler=FakeCompiler(CompileError("error: main.tex:12: Undefined control sequence", 12)))
    try:
        result = httpx.post(f"{base}/resume/{draft_id}/save", data={"tex": "\\oops"}, headers={"Accept": "application/json"}).json()
        page = httpx.get(f"{base}/?view=resume&draft={draft_id}").text
    finally:
        stop(server)
    assert result == {"ok": False, "error": "error: main.tex:12: Undefined control sequence", "line": 12}
    assert get_draft(conn, draft_id)["tex"] == "\\oops" and "Undefined control sequence" in page


def test_drafts_can_be_deleted(tmp_path):
    db = tmp_path / "agent.db"
    conn = connect(db)
    server, base = serve(db)
    try:
        draft_id = int(httpx.post(f"{base}/resume/new", data={"template": "compact", "source": "master"}).headers["location"].rsplit("=", 1)[1])
        assert httpx.post(f"{base}/resume/{draft_id}/delete").headers["location"] == "/?view=resume"
        missing = httpx.get(f"{base}/resume/{draft_id}.pdf").status_code
    finally:
        stop(server)
    assert get_draft(conn, draft_id) is None and missing == 404 and not (tmp_path / "resumes" / f"{draft_id}.pdf").exists()


def test_bad_requests_show_an_error(tmp_path):
    db = tmp_path / "agent.db"

    def missing():
        raise FileNotFoundError("config/master_resume.yaml")

    server, base = serve(db, loader=missing)
    try:
        assert httpx.post(f"{base}/resume/new", data={"template": "fancy", "source": "master"}).headers["location"] == "/?view=resume"
        assert "Choose a template" in httpx.get(f"{base}/?view=resume").text
        httpx.post(f"{base}/resume/new", data={"template": "classic", "source": "master"})
        assert "master_resume.yaml" in httpx.get(f"{base}/?view=resume").text
        assert httpx.post(f"{base}/resume/999/save", data={"tex": "x"}).status_code == 404
    finally:
        stop(server)


def pdf_compiler():
    from tests.test_ats import RESUME as TEXT, make_pdf
    lines = TEXT.replace("—", "-").replace("–", "-").splitlines()
    return lambda tex: make_pdf([lines])


def ats_llm():
    return StubLLM({"ats": lambda prompt: {"impact": 90, "seniority": 80, "clarity": 85, "keywords": ["Kafka", "Golang"], "fixes": ["Add a Golang project"]}})


def test_draft_is_scored_from_its_compiled_pdf_without_storing_anything(tmp_path):
    db = tmp_path / "agent.db"
    conn = connect(db)
    check = record_check(conn, "cv.pdf", "senior", ats_result(61), NOW)
    draft_id = create_draft(conn, "Classic · fixes", "classic", f"ats:{check}", NOW, tex="x")
    llm = ats_llm()
    server, base = serve(db, llm=llm, compiler=pdf_compiler())
    try:
        httpx.post(f"{base}/resume/{draft_id}/save", data={"tex": "x"}, headers={"Accept": "application/json"})
        editor = httpx.get(f"{base}/?view=resume&draft={draft_id}").text
        result = httpx.post(f"{base}/resume/{draft_id}/ats", data={"level": "senior", "jd": "Kafka and Golang"}).json()
    finally:
        stop(server)
    assert 'data-rb-ats' in editor and f'action="/resume/{draft_id}/ats"' in editor and 'value="senior" checked' in editor
    assert result["ok"] and result["baseline"] == {"score": 61, "filename": "cv.pdf"} and isinstance(result["score"], int)
    assert "ATS score" in result["html"] and "Golang" in result["html"] and "Add a Golang project" in result["html"]
    assert "Kafka and Golang" in llm.calls[0][2]
    assert conn.execute("SELECT COUNT(*) FROM ats_checks").fetchone()[0] == 1


def test_draft_scoring_needs_a_compiled_pdf_and_a_level(tmp_path):
    db = tmp_path / "agent.db"
    conn = connect(db)
    draft_id = create_draft(conn, "Draft", "classic", "master", NOW, tex="x")
    llm = ats_llm()
    server, base = serve(db, llm=llm, compiler=pdf_compiler())
    try:
        missing = httpx.post(f"{base}/resume/{draft_id}/ats", data={"level": "mid"}).json()
        httpx.post(f"{base}/resume/{draft_id}/save", data={"tex": "x"}, headers={"Accept": "application/json"})
        no_level = httpx.post(f"{base}/resume/{draft_id}/ats", data={"level": "boss"}).json()
        master = httpx.post(f"{base}/resume/{draft_id}/ats", data={"level": "mid"}).json()
        unknown = httpx.post(f"{base}/resume/999/ats", data={"level": "mid"}).status_code
    finally:
        stop(server)
    assert missing == {"ok": False, "error": "Compile the resume without errors first"}
    assert no_level == {"ok": False, "error": "Choose an experience level"}
    assert master["ok"] and master["baseline"] is None and unknown == 404 and len(llm.calls) == 1
