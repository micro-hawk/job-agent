import shutil
import subprocess
from pathlib import Path

import pytest

from agent.ats import extract_text
from agent.config import load_yaml
from agent.resume_builder import (
    ENGINES, TEMPLATES, CompileError, apply_changes, apply_tex_fix, compile_tex, create_draft, delete_draft, engine_of, get_draft,
    latex_escape, list_drafts, propose_changes, propose_tex_fix, render_tex, tex_targets, update_draft, with_engine,
)

from tests.fakes import NOW, StubLLM

EXAMPLES = Path(__file__).resolve().parent.parent / "config" / "examples"
RESUME = load_yaml(EXAMPLES / "master_resume.yaml")
PROFILE = load_yaml(EXAMPLES / "profile.yaml")


def test_latex_escape_covers_every_special_character():
    assert latex_escape(r"R&D 50% #1 a_b $5 {x} ~ ^ \ ") == r"R\&D 50\% \#1 a\_b \$5 \{x\} \textasciitilde{} \textasciicircum{} \textbackslash{} "
    assert latex_escape(None) == "" and latex_escape(3) == "3"


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_every_template_renders_the_whole_master_resume(template):
    tex = render_tex(template, RESUME, PROFILE)
    assert tex.startswith("%") and r"\begin{document}" in tex and tex.rstrip().endswith(r"\end{document}")
    for text in ("Alex Morgan", "alex.morgan@example.com", "Northwind Payments", "Contoso Health", "Jun 2024", "Present", "Ledgerlite", "Example Institute of Technology", "Spring Boot"):
        assert text in tex, text
    assert "Model Context Protocol (MCP)" in tex and "<<" not in tex and "<%" not in tex
    assert r"\href{https://github.com/alex-morgan-example}" in tex


def test_rendering_escapes_resume_text():
    resume = RESUME | {"headline": "R&D_lead 100%"}
    assert r"R\&D\_lead 100\%" in render_tex("classic", resume, PROFILE)


def test_resume_links_choose_which_profile_links_the_resume_shows():
    tex = render_tex("classic", RESUME, PROFILE | {"resume_links": ["linkedin", "portfolio"]})
    assert "alex-morgan-example}" in tex and "alex-morgan.example.com" in tex and "{github.com/alex-morgan-example}" not in tex
    assert "github.com/alex-morgan-example" in render_tex("classic", RESUME, PROFILE)


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_project_links_and_cgpa_render_when_given(template):
    resume = RESUME | {
        "projects": [{"name": "Ledgerlite", "stack": ["Python"], "url": "https://github.com/alex/ledgerlite", "bullets": []}, {"name": "Tripboard", "stack": [], "bullets": []}],
        "education": [{"institution": "Example Institute", "degree": "BE", "cgpa": "8.1/10", "start": "2018-08", "end": "2022-05"},
                      {"institution": "City College", "degree": "HSC, Science", "start": "2016", "end": "2018"}],
    }
    tex = render_tex(template, resume, PROFILE)
    assert r"\href{https://github.com/alex/ledgerlite}{github.com/alex/ledgerlite}" in tex and tex.count(r"\href{https://github.com/alex/") == 1
    assert "BE, CGPA 8.1/10" in tex and "{HSC, Science}" in tex and "2016 -- 2018" in tex


def test_render_rejects_unknown_templates():
    with pytest.raises(KeyError):
        render_tex("fancy", RESUME, PROFILE)


def rewrite(changes):
    return lambda prompt: {"changes": changes}


def test_propose_changes_keeps_only_honest_rewrites_of_existing_text():
    llm = StubLLM({"resume_rewrite": rewrite([
        {"target": "summary", "after": "Backend engineer with 4 years building Kafka systems.", "fix": "Tighten the summary"},
        {"target": "nw-kafka-events", "after": "Cut settlement lag by 90% by replacing a nightly batch with Kafka events.", "fix": "Quantify"},
        {"target": "ch-redis-cache", "after": "Added a Redis cache in front of the clinic directory API, cutting lookups.", "fix": "Show impact"},
        {"target": "made-up-bullet", "after": "Led a team of engineers.", "fix": "Leadership"},
        {"target": "headline", "after": "Senior Backend Engineer", "fix": "Unchanged"},
    ])})
    proposals = propose_changes(RESUME, ["Tighten the summary", "Quantify"], llm, "sonnet")
    assert [p["target"] for p in proposals] == ["summary", "ch-redis-cache"]
    assert proposals[1]["before"] == "Introduced a Redis cache in front of the clinic directory API." and proposals[1]["label"] == "Contoso Health bullet"
    task, model, prompt = llm.calls[0]
    assert (task, model) == ("resume_rewrite", "sonnet") and "Quantify" in prompt and "[ch-redis-cache]" in prompt and "example.com" not in prompt


def test_apply_changes_rewrites_only_accepted_targets_without_touching_the_original():
    changes = [{"target": "headline", "after": "Backend Engineer, Payments"}, {"target": "nw-search", "after": "Added cursor pagination."}]
    updated = apply_changes(RESUME, changes)
    assert updated["headline"] == "Backend Engineer, Payments" and updated["experience"][0]["bullets"][3]["text"] == "Added cursor pagination."
    assert RESUME["headline"] == "Senior Backend Engineer" and RESUME["experience"][0]["bullets"][3]["text"].startswith("Added cursor-based")


def test_compile_runs_tectonic_untrusted_and_returns_the_pdf():
    calls = []

    def runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        Path(kwargs["cwd"], "main.pdf").write_bytes(b"%PDF-fake")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert compile_tex("tex", runner=runner) == b"%PDF-fake"
    cmd, kwargs = calls[0]
    assert cmd[0] == "tectonic" and "--untrusted" in cmd and kwargs["timeout"] and Path(kwargs["cwd"], "main.tex").exists() is False


def test_compile_errors_carry_the_log_lines_and_line_number():
    stderr = ("note: Running TeX ...\nerror: something bad happened inside XeTeX; its output follows:\n"
              "error: main.tex:23: Undefined control sequence\nerror: th\nerror: halted on potentially-recoverable error as specified\n")
    with pytest.raises(CompileError) as raised:
        compile_tex("tex", runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", stderr))
    assert raised.value.line == 23 and str(raised.value) == "error: main.tex:23: Undefined control sequence"
    with pytest.raises(CompileError, match="took too long"):
        compile_tex("tex", runner=lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 1)))
    with pytest.raises(CompileError, match="not installed"):
        compile_tex("tex", runner=lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError()))


def test_drafts_are_saved_listed_updated_and_deleted(conn):
    first = create_draft(conn, "Classic from master", "classic", "master", NOW, tex="\\x")
    second = create_draft(conn, "Modern with fixes", "modern", "ats:3", NOW, proposals=[{"target": "summary"}])
    assert [d["name"] for d in list_drafts(conn)] == ["Modern with fixes", "Classic from master"]
    assert get_draft(conn, second)["proposals"] == [{"target": "summary"}] and get_draft(conn, second)["tex"] == ""
    update_draft(conn, first, "2026-10-07T10:00:00", tex="\\y", error="bad", error_line=4)
    draft = get_draft(conn, first)
    assert (draft["tex"], draft["error"], draft["error_line"], draft["updated_at"]) == ("\\y", "bad", 4, "2026-10-07T10:00:00")
    assert delete_draft(conn, first) and not delete_draft(conn, first) and get_draft(conn, first) is None


@pytest.mark.skipif(not shutil.which("tectonic"), reason="tectonic not installed")
@pytest.mark.parametrize("template", list(TEMPLATES))
def test_templates_compile_to_an_ats_readable_pdf(template):
    pdf = compile_tex(render_tex(template, RESUME, PROFILE), timeout=300)
    text, pages = extract_text("cv.pdf", pdf)
    assert pages == 1 and "Northwind Payments" in text and "alex.morgan@example.com" in text
    for heading in ("Experience", "Skills", "Education"):
        assert heading.lower() in text.lower()


def test_templates_declare_xelatex_and_the_engine_line_can_switch():
    tex = render_tex("classic", RESUME, PROFILE)
    assert tex.startswith("% !TEX program = xelatex\n") and engine_of(tex) == "xelatex"
    lua = with_engine(tex, "lualatex")
    assert lua.startswith("% !TEX program = lualatex\n") and lua.count("!TEX program") == 1 and engine_of(lua) == "lualatex"
    assert with_engine(lua, "xelatex") == tex
    assert engine_of("\\documentclass{article}") == "xelatex" and engine_of("% !TEX program = pdflatex\n") == "xelatex"
    assert with_engine("\\documentclass{article}", "lualatex") == "% !TEX program = lualatex\n\\documentclass{article}"
    assert set(ENGINES) == {"xelatex", "lualatex"}
    with pytest.raises(KeyError):
        with_engine(tex, "pdflatex")


def test_lualatex_drafts_compile_with_lualatex_without_shell_escape():
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        Path(kwargs["cwd"], "main.pdf").write_bytes(b"%PDF-lua")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert compile_tex("% !TEX program = lualatex\n\\x", runner=runner) == b"%PDF-lua"
    assert calls[0][0] == "lualatex" and "-no-shell-escape" in calls[0] and "-halt-on-error" in calls[0] and "-file-line-error" in calls[0]
    with pytest.raises(CompileError, match="LuaLaTeX is not installed"):
        compile_tex("% !TEX program = lualatex\n", runner=lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    log = "This is LuaHBTeX\n./main.tex:12: Undefined control sequence.\nl.12 \\oops\n"
    with pytest.raises(CompileError) as raised:
        compile_tex("% !TEX program = lualatex\n", runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, log, ""))
    assert raised.value.line == 12 and str(raised.value) == "./main.tex:12: Undefined control sequence."


def test_tex_targets_are_the_summary_and_plain_bullets_of_the_current_tex():
    tex = render_tex("classic", RESUME, PROFILE).replace(
        "Introduced a Redis cache in front of the clinic directory API.", r"Introduced a \textbf{Redis} cache in front of the clinic directory API.")
    targets = tex_targets(tex)
    assert targets["summary"]["text"].startswith("Backend engineer with 4 years") and targets["summary"]["label"] == "Summary"
    bullets = [t for key, t in targets.items() if key != "summary"]
    assert bullets[0]["label"] == "Northwind Payments bullet" and bullets[0]["text"].startswith("Owned the ledger microservice")
    assert any(t["label"] == "Ledgerlite project" for t in bullets)
    assert not any("Redis" in t["text"] for t in bullets)
    for target in targets.values():
        assert "\\" not in target["text"] and tex[target["start"]:target["end"]]


def test_tex_targets_unescape_special_characters():
    tex = "\\section{Summary}\nR\\&D lead, 50\\% faster\n\n\\resumeItem{Cut costs by 30\\% at R\\&D}\n"
    targets = tex_targets(tex)
    assert targets["summary"]["text"] == "R&D lead, 50% faster" and targets["b1"]["text"] == "Cut costs by 30% at R&D"


def fix_llm(changes):
    return StubLLM({"resume_fix": lambda prompt: {"changes": changes}})


def test_propose_tex_fix_rewrites_only_known_lines_without_new_numbers():
    tex = render_tex("classic", RESUME, PROFILE)
    targets = tex_targets(tex)
    bullet = next(key for key, t in targets.items() if "Redis cache" in t["text"])
    llm = fix_llm([
        {"target": "summary", "after": "Backend engineer with 4 years shipping Java & Spring Boot services at 50% lower latency.", "fix": "x"},
        {"target": f"[{bullet}]", "after": "Added a Redis cache in front of the clinic directory API to speed up lookups.", "fix": "x"},
        {"target": "b999", "after": "Led a team.", "fix": "x"},
    ])
    changes = propose_tex_fix(tex, RESUME, "Show impact in the Redis bullet", llm, "sonnet")
    assert [c["target"] for c in changes] == [bullet] and changes[0]["label"] == "Contoso Health bullet"
    assert changes[0]["before"] == "Introduced a Redis cache in front of the clinic directory API."
    task, model, prompt = llm.calls[0]
    assert (task, model) == ("resume_fix", "sonnet") and "Show impact in the Redis bullet" in prompt
    assert f"[{bullet}] (Contoso Health bullet) Introduced a Redis cache" in prompt and "Northwind Payments" in prompt and "example.com" not in prompt


def test_apply_tex_fix_replaces_lines_escaped_and_leaves_the_rest():
    tex = "% !TEX program = xelatex\n\\section{Summary}\nOld summary\n\n\\resumeItem{First}\n\\resumeItem{Second}\n"
    updated = apply_tex_fix(tex, [{"target": "b2", "after": "Second, now 100% better & faster"}, {"target": "summary", "after": "New summary"}])
    assert updated == "% !TEX program = xelatex\n\\section{Summary}\nNew summary\n\n\\resumeItem{First}\n\\resumeItem{Second, now 100\\% better \\& faster}\n"
