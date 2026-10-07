import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

import yaml

from agent.ats import (
    LEVELS, MAX_UPLOAD_BYTES, AtsInputError, clear_checks, delete_check, extract_text, find_check, fingerprint, get_check, recent_checks, record_check,
    score_resume,
)
from agent.config import load_settings
from agent.db import connect, update_job
from agent.llm import LLM, LLMError
from agent.profile import load_profile, load_resume
from agent.resume_builder import (
    TEMPLATES as RESUME_TEMPLATES, CompileError, apply_changes, apply_tex_fix, compile_tex, create_draft, delete_draft,
    engine_of, get_draft, list_drafts, propose_changes, propose_tex_fix, render_tex, tex_targets, update_draft,
)
from agent.models import ALERT_SOURCES
from agent.questionnaire import REASONS

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
CHART_DAYS = 14
NO_FIX = "This fix needs facts that aren't in your master resume. Edit it yourself."
PROJECT_ROOT = Path(__file__).parent.parent
INSTAHYRE_COMMAND = [sys.executable, "-m", "agent.run", "instahyre"]
INSTAHYRE_APPLIED_COMMAND = INSTAHYRE_COMMAND + ["--applied"]
INSTAHYRE_APPLY_COMMAND = INSTAHYRE_COMMAND + ["--apply"]
MANUAL_REFRESH_COMMAND = [sys.executable, "-m", "agent.run", "manual-refresh"]
QUESTIONNAIRES_COMMAND = [sys.executable, "-m", "agent.run", "questionnaires"]
UPLOAD_OVERHEAD_BYTES = 1024 * 1024
IDLE = {"running": False, "started_at": "", "last": ""}
ROW_LIMIT = 300
VIEWS = {
    "ready": ("Ready", ("ready",), "score DESC, id DESC", None),
    "manual": ("Manual 1-click", ("manual",), "id DESC", ALERT_SOURCES),
    "instahyre": ("Instahyre", ("manual",), "id", ("instahyre",)),
    "needs": ("Needs you", ("needs_you", "failed"), "score DESC, id DESC", None),
    "applied": ("Applied", ("applied",), "last_seen DESC, id DESC", None),
    "pipeline": ("In pipeline", ("candidate", "prescored"), "id DESC", None),
    "skipped": ("Skipped", ("filtered_out", "low_prescore", "below_threshold"), "last_seen DESC, id DESC", None),
}
MARKABLE_SOURCES = ALERT_SOURCES + ("instahyre",)
STATUS_ORDER = ("ready", "manual", "candidate", "prescored", "below_threshold", "low_prescore", "filtered_out")
_ENV = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
_ENV.filters["qp"] = quote_plus


STOPPED_MESSAGE = "stopped from the dashboard"
STOP_GRACE_SECONDS = 3


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


class Refresher:
    def __init__(self, command: list[str], cwd: Path):
        self.command = command
        self.cwd = cwd
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._started_at = ""
        self._last = ""
        self._process = None
        self._stopped = False

    def start(self, command: list[str] | None = None) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._stopped = False
            self._started_at = datetime.now().isoformat(timespec="seconds")
            self._thread = threading.Thread(target=self._run, args=(command or self.command,), daemon=True)
            self._thread.start()
            return True

    def _run(self, command: list[str]) -> None:
        try:
            process = subprocess.Popen(command, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            with self._lock:
                self._process = process
            stdout, stderr = process.communicate()
            lines = [line for line in (stdout + stderr).splitlines() if line.strip()]
            last = lines[-1] if lines else f"exited with code {process.returncode}"
        except OSError as exc:
            last = f"could not start: {exc}"
        with self._lock:
            self._last = STOPPED_MESSAGE if self._stopped else last
            self._process = None
            self._running = False

    def stop(self) -> bool:
        with self._lock:
            process = self._process
            if not self._running or process is None:
                return False
            self._stopped = True
        _signal_group(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        _signal_group(process.pid, signal.SIGKILL)
        return True

    def wait(self) -> None:
        if self._thread:
            self._thread.join()

    def status(self) -> dict:
        with self._lock:
            return {"running": self._running, "started_at": self._started_at, "last": self._last}


def describe_refresh(last: str) -> str:
    try:
        result = json.loads(last)
    except ValueError:
        return last
    if "questionnaire_submit" in result:
        outcome = result["questionnaire_submit"]
        words = {"submitted": "questionnaire submitted", "already": "questionnaire was already sent"}
        return f"{result['job_title']}: {words.get(outcome) or REASONS.get(outcome, outcome)}"
    if "questionnaires_new" in result:
        return (f"{result['questionnaires_new']} new questionnaires · {result.get('drafted', 0)} drafted for your review · "
                f"{result['already_sent']} already sent · {result['open_yourself']} to open yourself")
    if "linkedin_applied" in result:
        applied = result["linkedin_applied"] + result["naukri_applied"]
        return f"{applied} applied ({result['linkedin_applied']} LinkedIn, {result['naukri_applied']} Naukri) · {result['expired']} expired"
    if "instahyre_applied" in result:
        text = f"applied to {result['instahyre_applied']} · {result['already_applied']} were already applied · {result['left_for_you']} left for you to check"
        return text + (f" · stopped: {result['stopped']}" if result.get("stopped") else "")
    if "applied_on_instahyre" in result:
        return f"{result['applied_on_instahyre']} on your Instahyre Applied list · {result['marked_applied']} removed from this tab"
    text = f"{result['instahyre_matches']} matches on Instahyre · {result['new_in_tab']} new in this tab · {result.get('hidden_gone', 0)} hidden (applied or closed)"
    return text + (f" · {result['marked_applied']} marked applied" if "marked_applied" in result else "")


_ENV.filters["describe"] = describe_refresh


def summarize_run(stats: dict) -> str:
    parts = []
    ats = stats.get("ats") or {}
    if "fetched" in ats:
        parts.append(f"{ats['fetched']:,} fetched")
    if "new" in ats:
        parts.append(f"{ats['new']:,} new")
    if (stats.get("alerts") or {}).get("new"):
        parts.append(f"{stats['alerts']['new']} from alerts")
    if "ready" in (stats.get("score") or {}):
        parts.append(f"{stats['score']['ready']} ready")
    for key, label in (("applied", "applied"), ("needs_you", "needs you"), ("failed", "failed")):
        if key in stats:
            parts.append(f"{stats[key]} {label}")
    if "llm_spend_today_usd" in stats:
        parts.append(f"${stats['llm_spend_today_usd']:.2f} spent today")
    return " · ".join(parts) or "no changes"


_ENV.filters["summarize"] = summarize_run


def company_hue(name: str) -> int:
    return sum((index + 1) * ord(char) for index, char in enumerate(name.lower())) * 37 % 360


_ENV.filters["hue"] = company_hue
MARKET_LABELS = {"uk": "UK", "eu": "EU", "uae": "UAE"}


def market_label(market: str) -> str:
    return MARKET_LABELS.get(market) or market.replace("_", " ").capitalize()


_ENV.filters["market"] = market_label


def _view_count(conn: sqlite3.Connection, statuses: tuple, sources: tuple | None) -> int:
    sql = f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(statuses))})"
    params = list(statuses)
    if sources:
        sql += f" AND source IN ({','.join('?' * len(sources))})"
        params += sources
    return conn.execute(sql, params).fetchone()[0]


def _daily_applied(conn: sqlite3.Connection, today: date) -> list[dict]:
    start = today - timedelta(days=CHART_DAYS - 1)
    per_day = dict(conn.execute(
        "SELECT substr(ts, 1, 10), COUNT(*) FROM applications WHERE mode='live' AND status='applied' AND substr(ts, 1, 10) >= ? GROUP BY 1",
        (start.isoformat(),),
    ).fetchall())
    days = [start + timedelta(days=offset) for offset in range(CHART_DAYS)]
    return [{"day": day.isoformat(), "label": str(day.day), "n": per_day.get(day.isoformat(), 0)} for day in days]


def _source_check(conn: sqlite3.Connection, draft: dict | None) -> dict | None:
    source = draft["source"] if draft else ""
    return get_check(conn, int(source[4:])) if source.startswith("ats:") and source[4:].isdigit() else None


def _builder_context(conn: sqlite3.Connection, builder: dict) -> dict:
    draft_id = str(builder.get("draft") or "")
    draft = get_draft(conn, int(draft_id)) if draft_id.isdigit() else None
    return {
        "templates": RESUME_TEMPLATES,
        "drafts": list_drafts(conn),
        "checks": [check for check in recent_checks(conn) if check["result"].get("fixes")],
        "check": str(builder.get("check") or ""),
        "draft": draft,
        "source_check": _source_check(conn, draft),
        "levels": {key: value[0] for key, value in LEVELS.items()},
        "engine": engine_of(draft["tex"]) if draft else "xelatex",
        "has_pdf": bool(builder.get("has_pdf")),
        "error": builder.get("error", ""),
    }


def build_context(conn: sqlite3.Connection, view: str, today: date, refresh: dict | None = None, manual_refresh: dict | None = None, ats_error: str = "", builder: dict | None = None, ats_focus: dict | None = None) -> dict:
    view = view if view in VIEWS or view in ("referrals", "questionnaires", "ats", "resume") else "today"
    counts = {row["status"]: row["n"] for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
    spend = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS usd, COUNT(*) AS calls FROM llm_calls WHERE substr(ts, 1, 10)=?", (today.isoformat(),)
    ).fetchone()
    runs = [
        dict(row) | {"stats": json.loads(row["stats"]), "errors": json.loads(row["errors"])}
        for row in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5")
    ]
    jobs = []
    if view in VIEWS:
        _, statuses, order, sources = VIEWS[view]
        sql = f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(statuses))})"
        params = list(statuses)
        if sources:
            sql += f" AND source IN ({','.join('?' * len(sources))})"
            params += sources
        rows = conn.execute(f"{sql} ORDER BY {order} LIMIT {ROW_LIMIT}", params).fetchall()
        jobs = [dict(row) | {"detail": json.loads(row["score_detail"]) if row["score_detail"] else None} for row in rows]
    focus = ats_focus or {}
    ats_checks = recent_checks(conn) if view == "ats" else []
    focused = get_check(conn, int(focus["check"])) if view == "ats" and str(focus.get("check", "")).isdigit() else None
    ats_latest = focused or (ats_checks[0] if ats_checks else None)
    nav_counts = {key: _view_count(conn, value[1], value[3]) for key, value in VIEWS.items()}
    nav_counts["referrals"] = conn.execute("SELECT COUNT(*) FROM referrals WHERE status='todo'").fetchone()[0]
    nav_counts["questionnaires"] = conn.execute("SELECT COUNT(*) FROM questionnaires WHERE status IN ('draft', 'manual')").fetchone()[0]
    daily = _daily_applied(conn, today)
    referrals = []
    if view == "referrals":
        referrals = [dict(row) for row in conn.execute(
            "SELECT r.*, j.title, j.url FROM referrals r JOIN jobs j ON j.id = r.job_id ORDER BY r.status = 'sent', r.company"
        )]
    questionnaires = []
    if view == "questionnaires":
        questionnaires = [
            dict(row) | {"questions": json.loads(row["questions"]), "answers": json.loads(row["answers"]), "suggestions": json.loads(row["suggestions"])}
            for row in conn.execute("SELECT * FROM questionnaires ORDER BY CASE status WHEN 'draft' THEN 0 WHEN 'manual' THEN 1 ELSE 2 END, id DESC")
        ]
    return {
        "view": view,
        "views": {key: value[0] for key, value in VIEWS.items()} | {"referrals": "Referrals", "questionnaires": "Questionnaires"},
        "statuses": STATUS_ORDER,
        "counts": counts,
        "spend": dict(spend),
        "runs": runs,
        "jobs": jobs,
        "referrals": referrals,
        "questionnaires": questionnaires,
        "ats_checks": ats_checks,
        "ats_latest": ats_latest,
        "ats_saved": bool(ats_latest and focus.get("saved") and str(ats_latest["id"]) == str(focus.get("check"))),
        "ats_levels": {key: value[0] for key, value in LEVELS.items()},
        "ats_error": ats_error,
        "builder": _builder_context(conn, builder or {}) if view == "resume" else {},
        "nav_counts": nav_counts,
        "daily": daily,
        "daily_max": max([day["n"] for day in daily] + [1]),
        "applied_today": daily[-1]["n"],
        "today": today,
        "refresh": refresh or IDLE,
        "manual_refresh": manual_refresh or IDLE,
    }


def render(conn: sqlite3.Connection, view: str, today: date, refresh: dict | None = None, manual_refresh: dict | None = None, ats_error: str = "", builder: dict | None = None, ats_focus: dict | None = None) -> str:
    return _ENV.get_template("index.html").render(**build_context(conn, view, today, refresh, manual_refresh, ats_error, builder, ats_focus))


def mark_applied(conn: sqlite3.Connection, job_id: int) -> bool:
    marks = ",".join("?" * len(MARKABLE_SOURCES))
    row = conn.execute(f"SELECT id FROM jobs WHERE id=? AND status='manual' AND source IN ({marks})", (job_id, *MARKABLE_SOURCES)).fetchone()
    if not row:
        return False
    update_job(conn, job_id, status="applied", reason="applied manually")
    return True


def mark_referral_sent(conn: sqlite3.Connection, referral_id: int, now: str) -> bool:
    cursor = conn.execute("UPDATE referrals SET status='sent', sent_at=? WHERE id=? AND status='todo'", (now, referral_id))
    conn.commit()
    return cursor.rowcount == 1


def save_questionnaire_answers(conn: sqlite3.Connection, row_id: int, form: dict, submit: bool) -> bool | None:
    row = conn.execute("SELECT questions FROM questionnaires WHERE id=? AND status='draft'", (row_id,)).fetchone()
    if not row:
        return None
    questions = json.loads(row["questions"])
    answers = {str(question["id"]): form.get(f"a_{question['id']}", [""])[0].strip() for question in questions}
    complete = all(answers[str(question["id"])].strip() for question in questions if question["required"])
    reason = REASONS["blank"] if submit and not complete else ""
    conn.execute("UPDATE questionnaires SET answers=?, reason=? WHERE id=?", (json.dumps(answers), reason, row_id))
    conn.commit()
    return submit and complete


def parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[str | None, bytes]]:
    message = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    if not message.is_multipart():
        return {}
    fields = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return fields


def _field(fields: dict, name: str) -> str:
    return (fields.get(name) or (None, b""))[1].decode("utf-8", "replace")


def check_resume(conn: sqlite3.Connection, fields: dict, llm, model: str, now: str) -> tuple[dict | None, bool, str]:
    level, jd = _field(fields, "level"), _field(fields, "jd")
    filename, data = fields.get("resume") or (None, b"")
    if level not in LEVELS:
        return None, False, "choose an experience level"
    if not filename:
        return None, False, "choose a resume file to upload"
    if len(data) > MAX_UPLOAD_BYTES:
        return None, False, "that file is larger than 5 MB"
    key = fingerprint(data, level, jd)
    saved = None if _field(fields, "rescore") == "1" else find_check(conn, key)
    if saved:
        return saved, True, ""
    try:
        text, pages = extract_text(filename, data)
        result = score_resume(text, pages, level, jd, llm, model)
    except AtsInputError as exc:
        return None, False, str(exc)
    except LLMError as exc:
        return None, False, f"scoring failed: {exc}"
    return get_check(conn, record_check(conn, Path(filename).name, level, result, now, fingerprint=key)), False, ""


def start_resume(conn: sqlite3.Connection, form: dict, loader, llm_factory, model: str, now: str) -> tuple[int | None, str]:
    template, source = form.get("template", [""])[0], form.get("source", ["master"])[0]
    if template not in RESUME_TEMPLATES:
        return None, "Choose a template to start from"
    try:
        resume, profile = loader()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return None, f"could not read your master resume: {exc}"
    label = RESUME_TEMPLATES[template][0]
    if source == "master":
        return create_draft(conn, f"{label} · master resume", template, "master", now, tex=render_tex(template, resume, profile)), ""
    check = get_check(conn, int(source)) if source.isdigit() else None
    if not check or not check["result"].get("fixes"):
        return None, "That ATS check has no fixes to apply"
    try:
        proposals = propose_changes(resume, check["result"]["fixes"], llm_factory(conn), model)
    except LLMError as exc:
        return None, f"rewriting failed: {exc}"
    return create_draft(conn, f"{label} · fixes from {check['filename']}", template, f"ats:{check['id']}", now, proposals=proposals), ""


def generate_resume(conn: sqlite3.Connection, draft: dict, accepted: set[str], loader, now: str) -> str:
    proposals = [proposal | {"accepted": proposal["target"] in accepted} for proposal in draft["proposals"]]
    resume, profile = loader()
    update_draft(conn, draft["id"], now, proposals=proposals)
    return render_tex(draft["template"], apply_changes(resume, [proposal for proposal in proposals if proposal["accepted"]]), profile)


def score_draft(conn: sqlite3.Connection, draft: dict, form: dict, pdf: Path, llm, model: str) -> dict:
    level, jd = form.get("level", [""])[0], form.get("jd", [""])[0]
    if draft["error"] or not pdf.is_file():
        return {"ok": False, "error": "Compile the resume without errors first"}
    if level not in LEVELS:
        return {"ok": False, "error": "Choose an experience level"}
    try:
        text, pages = extract_text("resume.pdf", pdf.read_bytes())
        result = score_resume(text, pages, level, jd, llm, model)
    except AtsInputError as exc:
        return {"ok": False, "error": str(exc)}
    except LLMError as exc:
        return {"ok": False, "error": f"scoring failed: {exc}"}
    source = _source_check(conn, draft)
    baseline = {"score": source["score"], "filename": source["filename"]} if source else None
    html = _ENV.get_template("_draft_score.html").render(r=result, baseline=baseline, level_label=LEVELS[level][0], draft_id=draft["id"])
    return {"ok": True, "score": result["score"], "baseline": baseline, "html": html}


def fix_draft(form: dict, loader, llm, model: str) -> dict:
    tex, fix = form.get("tex", [""])[0], form.get("fix", [""])[0].strip()
    if not fix:
        return {"ok": False, "error": "Choose a fix to apply"}
    if not tex_targets(tex):
        return {"ok": False, "error": "No summary or bullet lines found to rewrite in this LaTeX"}
    try:
        resume, _ = loader()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return {"ok": False, "error": f"could not read your master resume: {exc}"}
    try:
        changes = propose_tex_fix(tex, resume, fix, llm, model)
    except LLMError as exc:
        return {"ok": False, "error": f"rewriting failed: {exc}"}
    if not changes:
        return {"ok": True, "changes": [], "tex": tex, "message": NO_FIX}
    return {"ok": True, "tex": apply_tex_fix(tex, changes), "changes": [{key: change[key] for key in ("label", "before", "after")} for change in changes]}


def compile_draft(conn: sqlite3.Connection, draft_id: int, tex: str, compiler, pdf_dir: Path, now: str) -> dict:
    try:
        pdf = compiler(tex)
    except CompileError as exc:
        update_draft(conn, draft_id, now, tex=tex, error=str(exc), error_line=exc.line)
        return {"ok": False, "error": str(exc), "line": exc.line}
    pdf_dir.mkdir(parents=True, exist_ok=True)
    (pdf_dir / f"{draft_id}.pdf").write_bytes(pdf)
    update_draft(conn, draft_id, now, tex=tex, error="", error_line=None)
    return {"ok": True, "error": "", "line": None}


def make_server(db_path, host: str, port: int, refresher: Refresher | None = None, applied_command: list[str] = INSTAHYRE_APPLIED_COMMAND, apply_command: list[str] = INSTAHYRE_APPLY_COMMAND, manual_refresher: Refresher | None = None, questionnaires_command: list[str] = QUESTIONNAIRES_COMMAND, llm_factory=None, ats_model: str = "sonnet", resume_loader=None, compiler=compile_tex) -> ThreadingHTTPServer:
    refresher = refresher or Refresher(INSTAHYRE_COMMAND, PROJECT_ROOT)
    manual_refresher = manual_refresher or Refresher(MANUAL_REFRESH_COMMAND, PROJECT_ROOT)
    llm_factory = llm_factory or (lambda conn: LLM(conn, load_settings()["daily_budget_usd"]))
    ats_state = {"error": ""}
    resume_state = {"error": ""}
    resume_loader = resume_loader or (lambda: (load_resume(), load_profile()))
    pdf_dir = Path(db_path).parent / "resumes"
    instahyre_actions = {"refresh": None, "applied": applied_command, "apply": apply_command}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            parts = urlparse(self.path).path.strip("/").split("/")
            if parts == ["manual", "refresh"]:
                manual_refresher.start()
                self.send_response(303)
                self.send_header("Location", "/?view=manual")
                self.end_headers()
                return
            if parts == ["ats", "clear"] or (len(parts) == 3 and parts[0] == "ats" and parts[1].isdigit() and parts[2] == "delete"):
                conn = connect(db_path)
                try:
                    clear_checks(conn) if parts[1] == "clear" else delete_check(conn, int(parts[1]))
                finally:
                    conn.close()
                self.redirect("/?view=ats")
                return
            if parts == ["ats", "check"]:
                length = int(self.headers.get("Content-Length") or 0)
                check, saved = None, False
                if length > MAX_UPLOAD_BYTES + UPLOAD_OVERHEAD_BYTES:
                    ats_state["error"] = "that file is larger than 5 MB"
                    self.close_connection = True
                else:
                    fields = parse_multipart(self.headers.get("Content-Type") or "", self.rfile.read(length))
                    conn = connect(db_path)
                    try:
                        check, saved, ats_state["error"] = check_resume(conn, fields, llm_factory(conn), ats_model, datetime.now().isoformat(timespec="seconds"))
                    finally:
                        conn.close()
                location = f"/?view=ats&check={check['id']}&saved=1" if saved else "/?view=ats"
                if "application/json" in (self.headers.get("Accept") or ""):
                    self.send_json({"location": location, "saved": saved, "check": check and check["id"],
                                    "score": check and check["score"], "created_at": check and check["created_at"]})
                    return
                self.redirect(location)
                return
            if parts[0] == "resume":
                self.resume_post(parts)
                return
            if parts == ["questionnaires", "fetch"]:
                refresher.start(questionnaires_command)
                self.redirect("/?view=questionnaires")
                return
            if len(parts) == 3 and parts[0] == "questionnaires" and parts[1].isdigit() and parts[2] == "submit":
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                conn = connect(db_path)
                try:
                    ready = save_questionnaire_answers(conn, int(parts[1]), form, form.get("action", [""])[0] == "submit")
                finally:
                    conn.close()
                if ready is None:
                    self.send_error(404)
                    return
                if ready:
                    refresher.start(questionnaires_command + ["--submit", parts[1]])
                self.redirect("/?view=questionnaires")
                return
            if parts == ["instahyre", "stop"]:
                refresher.stop()
                self.send_response(303)
                self.send_header("Location", "/?view=instahyre")
                self.end_headers()
                return
            if len(parts) == 2 and parts[0] == "instahyre" and parts[1] in instahyre_actions:
                refresher.start(instahyre_actions[parts[1]])
                self.send_response(303)
                self.send_header("Location", "/?view=instahyre")
                self.end_headers()
                return
            actions = {("jobs", "applied"): lambda conn, item: mark_applied(conn, item),
                       ("referrals", "sent"): lambda conn, item: mark_referral_sent(conn, item, datetime.now().isoformat(timespec="seconds"))}
            if len(parts) != 3 or (parts[0], parts[2]) not in actions or not parts[1].isdigit():
                self.send_error(404)
                return
            conn = connect(db_path)
            try:
                found = actions[(parts[0], parts[2])](conn, int(parts[1]))
            finally:
                conn.close()
            if not found:
                self.send_error(404)
                return
            self.send_response(303)
            self.send_header("Location", self.headers.get("Referer") or "/?view=instahyre")
            self.end_headers()

        def resume_post(self, parts: list[str]) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            now = datetime.now().isoformat(timespec="seconds")
            conn = connect(db_path)
            try:
                if parts == ["resume", "new"]:
                    draft_id, resume_state["error"] = start_resume(conn, form, resume_loader, llm_factory, ats_model, now)
                    if draft_id and get_draft(conn, draft_id)["tex"]:
                        compile_draft(conn, draft_id, get_draft(conn, draft_id)["tex"], compiler, pdf_dir, now)
                    self.redirect(f"/?view=resume&draft={draft_id}" if draft_id else "/?view=resume")
                    return
                draft = get_draft(conn, int(parts[1])) if len(parts) == 3 and parts[1].isdigit() else None
                if not draft or parts[2] not in ("generate", "save", "delete", "ats", "fix"):
                    self.send_error(404)
                    return
                if parts[2] == "delete":
                    delete_draft(conn, draft["id"])
                    (pdf_dir / f"{draft['id']}.pdf").unlink(missing_ok=True)
                    self.redirect("/?view=resume")
                    return
                if parts[2] == "generate":
                    if not draft["tex"]:
                        tex = generate_resume(conn, draft, set(form.get("accept", [])), resume_loader, now)
                        compile_draft(conn, draft["id"], tex, compiler, pdf_dir, now)
                    self.redirect(f"/?view=resume&draft={draft['id']}")
                    return
                if parts[2] == "fix":
                    self.send_json(fix_draft(form, resume_loader, llm_factory(conn), ats_model))
                    return
                if parts[2] == "ats":
                    self.send_json(score_draft(conn, draft, form, pdf_dir / f"{draft['id']}.pdf", llm_factory(conn), ats_model))
                    return
                result = compile_draft(conn, draft["id"], form.get("tex", [""])[0], compiler, pdf_dir, now)
            finally:
                conn.close()
            if "application/json" in (self.headers.get("Accept") or ""):
                self.send_json(result)
                return
            self.redirect(f"/?view=resume&draft={draft['id']}")

        def send_json(self, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_draft_file(self, name: str, download: bool) -> None:
            stem, _, kind = name.partition(".")
            conn = connect(db_path)
            try:
                draft = get_draft(conn, int(stem)) if stem.isdigit() and kind in ("pdf", "tex") else None
            finally:
                conn.close()
            pdf = pdf_dir / f"{stem}.pdf"
            if not draft or (kind == "pdf" and not pdf.is_file()):
                self.send_error(404)
                return
            body = pdf.read_bytes() if kind == "pdf" else draft["tex"].encode("utf-8")
            filename = "resume-" + "".join(char if char.isalnum() else "-" for char in draft["name"].lower()).strip("-")
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf" if kind == "pdf" else "application/x-tex; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            disposition = "attachment" if download or kind == "tex" else "inline"
            self.send_header("Content-Disposition", f'{disposition}; filename="{filename[:60]}.{kind}"')
            self.end_headers()
            self.wfile.write(body)

        def redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def send_static(self, name: str) -> None:
            path = STATIC / name
            if "/" in name or "\\" in name or name.startswith(".") or path.suffix not in STATIC_TYPES or not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES[path.suffix])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path.startswith("/static/"):
                self.send_static(unquote(url.path[len("/static/"):]))
                return
            query = parse_qs(url.query)
            if url.path.startswith("/resume/"):
                self.send_draft_file(url.path[len("/resume/"):], "download" in query)
                return
            if url.path != "/":
                self.send_error(404)
                return
            view = query.get("view", ["today"])[0]
            conn = connect(db_path)
            try:
                error = ats_state["error"] if view == "ats" else ""
                if view == "ats":
                    ats_state["error"] = ""
                builder = None
                if view == "resume":
                    draft = query.get("draft", [""])[0]
                    builder = {"draft": draft, "check": query.get("check", [""])[0], "error": resume_state["error"], "has_pdf": draft.isdigit() and (pdf_dir / f"{draft}.pdf").is_file()}
                    resume_state["error"] = ""
                focus = {"check": query.get("check", [""])[0], "saved": query.get("saved", [""])[0] == "1"}
                body = render(conn, view, date.today(), refresher.status(), manual_refresher.status(), error, builder, focus).encode("utf-8")
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve(db_path, host: str, port: int, ats_model: str = "sonnet") -> None:
    server = make_server(db_path, host, port, ats_model=ats_model)
    print(f"Dashboard on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
