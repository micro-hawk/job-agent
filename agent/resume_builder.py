import copy
import json
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES = {
    "classic": ("Classic", "Serif, centred header and ruled sections. Adapted from Jake Gutierrez's MIT-licensed resume."),
    "modern": ("Modern", "Sans-serif with a coloured accent on the name and section rules."),
    "compact": ("Compact", "Tighter margins and spacing to keep a long history on one page."),
}
ENGINES = {"xelatex": "XeLaTeX", "lualatex": "LuaLaTeX"}
ENGINE_LINE = re.compile(r"\A%\s*!TEX\s+(?:TS-)?program\s*=\s*(\w+)[^\n]*\n?")
COMPILE_TIMEOUT = 90
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
TEX_LINE = re.compile(r"\.tex:(\d+):")
SPECIALS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}
SPECIAL = re.compile("|".join(re.escape(char) for char in SPECIALS))
UNESCAPE = re.compile("|".join(re.escape(escaped) for escaped in sorted(SPECIALS.values(), key=len, reverse=True)))
PLAIN = {escaped: char for char, escaped in SPECIALS.items()}
FIX_SYSTEM = (
    "You apply one fix from an ATS review to a resume. You get the fix, the editable resume lines as tagged plain text "
    "and the person's master resume, which holds every fact you may use. Rewrite only tagged lines, and only the ones the "
    "fix is about. Rules: reword, tighten or reorder what the line already says, or bring in wording that the master "
    "resume states about that same role or project; never add employers, roles, tools, metrics, team sizes or results "
    "that the master resume does not state; never introduce a number that is not already there; plain text only, no "
    "markdown or LaTeX; keep each bullet to one or two lines. If the fix needs facts the master resume does not have, "
    "return no changes."
)
SYSTEM = (
    "You improve resume wording. You get a resume as tagged lines and a list of fixes from an ATS review. Propose "
    "rewrites that apply the fixes, only for the tagged lines: headline, summary and bullet ids. Rules: reword, tighten "
    "or reorder what the line already says; never add employers, roles, tools, metrics, team sizes or results that the "
    "resume does not already state; never introduce a number that is not already in the resume; plain text only, no "
    "markdown or LaTeX; keep each bullet to one or two lines; one change per target; skip lines that are already good. "
    "For each change, give the fix it addresses."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"target": {"type": "string"}, "after": {"type": "string"}, "fix": {"type": "string"}},
                "required": ["target", "after", "fix"],
            },
        },
    },
    "required": ["changes"],
}


class CompileError(RuntimeError):
    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


class Raw(str):
    pass


def latex_escape(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Raw):
        return value
    return SPECIAL.sub(lambda match: SPECIALS[match.group()], str(value))


def latex_url(value) -> Raw:
    return Raw(re.sub(r"([%#\\{}])", r"\\\1", str(value or "")))


def month(value) -> str:
    if str(value).lower() == "present":
        return "Present"
    try:
        return datetime.strptime(str(value), "%Y-%m").strftime("%b %Y")
    except ValueError:
        return str(value)


def display_url(value) -> str:
    return re.sub(r"^https?://(www\.)?", "", str(value or "")).rstrip("/")


_ENV = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "resume_templates"),
    block_start_string="<%", block_end_string="%>",
    variable_start_string="<<", variable_end_string=">>",
    comment_start_string="<#", comment_end_string="#>",
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
    autoescape=False, finalize=latex_escape, undefined=StrictUndefined,
)
_ENV.filters.update(url=latex_url, month=month, display_url=display_url)


def render_tex(template: str, resume: dict, profile: dict) -> str:
    if template not in TEMPLATES:
        raise KeyError(template)
    location = profile.get("location") or {}
    contact = {
        "name": profile.get("name", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "location": ", ".join(part for part in (location.get("city"), location.get("country")) if part),
        "links": [url for url in (profile.get("links") or {}).values() if url],
    }
    return with_engine("", "xelatex") + (_ENV.get_template(f"{template}.tex.j2").render(contact=contact, resume=resume | {
        "skills": resume.get("skills") or [], "experience": resume.get("experience") or [],
        "projects": resume.get("projects") or [], "education": resume.get("education") or [],
    }))


def engine_of(tex: str) -> str:
    match = ENGINE_LINE.match(tex)
    return match.group(1).lower() if match and match.group(1).lower() in ENGINES else "xelatex"


def with_engine(tex: str, engine: str) -> str:
    if engine not in ENGINES:
        raise KeyError(engine)
    return f"% !TEX program = {engine}\n" + ENGINE_LINE.sub("", tex, count=1)


def _group(tex: str, start: int) -> int | None:
    depth = 0
    index = start
    while index < len(tex):
        char = tex[index]
        if char == "\\":
            index += 2
            continue
        depth += {"{": 1, "}": -1}.get(char, 0)
        if depth == 0:
            return index
        index += 1
    return None


def _plain(latex: str) -> str | None:
    if re.search(r"[\\{}]", UNESCAPE.sub("", latex)):
        return None
    return " ".join(UNESCAPE.sub(lambda match: PLAIN[match.group()], latex).split())


def _heading(tex: str, command: str) -> str:
    start = tex.find("{", tex.find(command) + len(command) - 1)
    args = []
    while len(args) < 3 and start < len(tex) and tex[start] == "{":
        end = _group(tex, start)
        if end is None:
            break
        args.append(tex[start + 1:end])
        start = end + 1
    if command == "\\resumeProjectHeading":
        name = re.search(r"\\textbf\{([^{}]*)\}", args[0]) if args else None
        return f"{_plain(name.group(1)) if name else 'Project'} project"
    return f"{_plain(args[2]) if len(args) > 2 else 'Experience'} bullet"


def tex_targets(tex: str) -> dict[str, dict]:
    targets = {}
    summary = re.search(r"\\section\{Summary\}[ \t]*\n(.+?)(?=\n[ \t]*\n|\n\\|\Z)", tex, re.S)
    if summary and (text := _plain(summary.group(1))):
        targets["summary"] = {"label": "Summary", "text": text, "start": summary.start(1), "end": summary.end(1)}
    label, count = "Bullet", 0
    for match in re.finditer(r"\\resumeSubheading|\\resumeProjectHeading|\\resumeItem\{", tex):
        if match.group() != "\\resumeItem{":
            label = _heading(tex[match.start():], match.group())
            continue
        if tex[:match.start()].endswith("newcommand{"):
            continue
        end = _group(tex, match.end() - 1)
        if end is None:
            continue
        count += 1
        if text := _plain(tex[match.end():end]):
            targets[f"b{count}"] = {"label": label, "text": text, "start": match.end(), "end": end}
    return targets


def propose_tex_fix(tex: str, resume: dict, fix: str, llm, model: str) -> list[dict]:
    targets = tex_targets(tex)
    known_numbers = set(NUMBER.findall(json.dumps(resume, ensure_ascii=False)))
    lines = "\n".join(f"[{key}] ({target['label']}) {target['text']}" for key, target in targets.items())
    prompt = f"Fix to apply:\n{fix}\n\nEditable lines:\n{lines}\n\nMaster resume:\n{_resume_lines(resume)}"
    changes, seen = [], set()
    for change in llm.call("resume_fix", model, FIX_SYSTEM, prompt, SCHEMA).get("changes", []):
        target, after = str(change.get("target", "")).strip().strip("[]"), " ".join(str(change.get("after", "")).split())
        if target not in targets or target in seen or not after or after == targets[target]["text"]:
            continue
        if set(NUMBER.findall(after)) - known_numbers - set(NUMBER.findall(targets[target]["text"])):
            continue
        seen.add(target)
        changes.append({"target": target, "label": targets[target]["label"], "before": targets[target]["text"], "after": after})
    return changes


def apply_tex_fix(tex: str, changes: list[dict]) -> str:
    targets = tex_targets(tex)
    edits = sorted(((targets[c["target"]], c["after"]) for c in changes if c["target"] in targets), key=lambda edit: edit[0]["start"], reverse=True)
    for target, after in edits:
        tex = tex[:target["start"]] + latex_escape(after) + tex[target["end"]:]
    return tex


def _targets(resume: dict) -> dict[str, tuple[str, str]]:
    targets = {"headline": ("Headline", resume.get("headline", "")), "summary": ("Summary", resume.get("summary", ""))}
    for job in resume.get("experience") or []:
        for bullet in job.get("bullets") or []:
            targets[bullet["id"]] = (f"{job['company']} bullet", bullet["text"])
    for project in resume.get("projects") or []:
        for bullet in project.get("bullets") or []:
            targets[bullet["id"]] = (f"{project['name']} project", bullet["text"])
    return {key: value for key, value in targets.items() if value[1]}


def _resume_lines(resume: dict) -> str:
    lines = [f"[headline] {resume.get('headline', '')}", f"[summary] {resume.get('summary', '')}"]
    for job in resume.get("experience") or []:
        lines.append(f"\n{job['title']}, {job['company']} ({month(job['start'])} – {month(job['end'])})")
        lines += [f"[{bullet['id']}] {bullet['text']}" for bullet in job.get("bullets") or []]
    for project in resume.get("projects") or []:
        lines.append(f"\nProject: {project['name']} ({', '.join(project.get('stack') or [])})")
        lines += [f"[{bullet['id']}] {bullet['text']}" for bullet in project.get("bullets") or []]
    skills = "; ".join(f"{group['group']}: {', '.join(group['items'])}" for group in resume.get("skills") or [])
    return "\n".join(lines) + f"\n\nSkills: {skills}"


def propose_changes(resume: dict, fixes: list[str], llm, model: str) -> list[dict]:
    targets = _targets(resume)
    known_numbers = set(NUMBER.findall(json.dumps(resume, ensure_ascii=False)))
    prompt = "Fixes to apply:\n" + "\n".join(f"- {fix}" for fix in fixes) + "\n\nResume:\n" + _resume_lines(resume)
    proposals, seen = [], set()
    for change in llm.call("resume_rewrite", model, SYSTEM, prompt, SCHEMA).get("changes", []):
        target, after = str(change.get("target", "")).strip().strip("[]"), " ".join(str(change.get("after", "")).split())
        if target not in targets or target in seen or not after or after == targets[target][1]:
            continue
        if set(NUMBER.findall(after)) - known_numbers:
            continue
        seen.add(target)
        label, before = targets[target]
        proposals.append({"target": target, "label": label, "before": before, "after": after, "fix": str(change.get("fix", "")).strip()})
    return proposals


def apply_changes(resume: dict, changes: list[dict]) -> dict:
    updated = copy.deepcopy(resume)
    rewrites = {change["target"]: change["after"] for change in changes}
    for key in ("headline", "summary"):
        if key in rewrites:
            updated[key] = rewrites[key]
    for section in ("experience", "projects"):
        for entry in updated.get(section) or []:
            for bullet in entry.get("bullets") or []:
                bullet["text"] = rewrites.get(bullet["id"], bullet["text"])
    return updated


def compile_tex(tex: str, runner=subprocess.run, timeout: int = COMPILE_TIMEOUT, binary: str = "tectonic") -> bytes:
    with tempfile.TemporaryDirectory(prefix="resume-") as workdir:
        Path(workdir, "main.tex").write_text(tex, encoding="utf-8")
        if engine_of(tex) == "lualatex":
            cmd = ["lualatex", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-no-shell-escape", "main.tex"]
            missing = "LuaLaTeX is not installed. Install MacTeX or BasicTeX, or switch the engine to XeLaTeX"
        else:
            cmd = [binary, "--untrusted", "--chatter", "minimal", "--outdir", workdir, "main.tex"]
            missing = "Tectonic is not installed. Install it with: brew install tectonic"
        try:
            result = runner(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise CompileError(f"LaTeX took too long to compile (over {timeout} seconds)") from exc
        except FileNotFoundError as exc:
            raise CompileError(missing) from exc
        pdf = Path(workdir, "main.pdf")
        if result.returncode != 0 or not pdf.exists():
            log = f"{result.stdout or ''}\n{result.stderr or ''}"
            errors = [line.strip() for line in log.splitlines() if line.strip().lower().startswith("error") or TEX_LINE.search(line)]
            errors = [error for error in errors if TEX_LINE.search(error)] or errors
            line = next((int(match.group(1)) for error in errors if (match := TEX_LINE.search(error))), None)
            raise CompileError("\n".join(errors[:6]) or log.strip()[-800:] or "LaTeX could not compile this file", line)
        return pdf.read_bytes()


def _draft(row) -> dict | None:
    return dict(row) | {"proposals": json.loads(row["proposals"])} if row else None


def create_draft(conn: sqlite3.Connection, name: str, template: str, source: str, now: str, tex: str = "", proposals: list | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO resume_drafts (name, template, source, tex, proposals, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, template, source, tex, json.dumps(proposals or []), now, now),
    )
    conn.commit()
    return cursor.lastrowid


def get_draft(conn: sqlite3.Connection, draft_id: int) -> dict | None:
    return _draft(conn.execute("SELECT * FROM resume_drafts WHERE id = ?", (draft_id,)).fetchone())


def list_drafts(conn: sqlite3.Connection) -> list[dict]:
    return [_draft(row) for row in conn.execute("SELECT * FROM resume_drafts ORDER BY updated_at DESC, id DESC")]


def update_draft(conn: sqlite3.Connection, draft_id: int, now: str, **fields) -> None:
    allowed = {key: value for key, value in fields.items() if key in {"tex", "error", "error_line", "proposals", "name"}}
    if "proposals" in allowed:
        allowed["proposals"] = json.dumps(allowed["proposals"])
    assignments = ", ".join(f"{key} = ?" for key in allowed)
    conn.execute(f"UPDATE resume_drafts SET {assignments}, updated_at = ? WHERE id = ?", (*allowed.values(), now, draft_id))
    conn.commit()


def delete_draft(conn: sqlite3.Connection, draft_id: int) -> bool:
    deleted = conn.execute("DELETE FROM resume_drafts WHERE id = ?", (draft_id,)).rowcount
    conn.commit()
    return bool(deleted)
