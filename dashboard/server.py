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

from agent.ats import LEVELS, MAX_UPLOAD_BYTES, AtsInputError, clear_checks, delete_check, extract_text, recent_checks, record_check, score_resume
from agent.config import load_settings
from agent.db import connect, update_job
from agent.llm import LLM, LLMError
from agent.models import ALERT_SOURCES
from agent.questionnaire import REASONS

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
CHART_DAYS = 14
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


def build_context(conn: sqlite3.Connection, view: str, today: date, refresh: dict | None = None, manual_refresh: dict | None = None, ats_error: str = "") -> dict:
    view = view if view in VIEWS or view in ("referrals", "questionnaires", "ats") else "today"
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
        "ats_checks": recent_checks(conn) if view == "ats" else [],
        "ats_levels": {key: value[0] for key, value in LEVELS.items()},
        "ats_error": ats_error,
        "nav_counts": nav_counts,
        "daily": daily,
        "daily_max": max([day["n"] for day in daily] + [1]),
        "applied_today": daily[-1]["n"],
        "today": today,
        "refresh": refresh or IDLE,
        "manual_refresh": manual_refresh or IDLE,
    }


def render(conn: sqlite3.Connection, view: str, today: date, refresh: dict | None = None, manual_refresh: dict | None = None, ats_error: str = "") -> str:
    return _ENV.get_template("index.html").render(**build_context(conn, view, today, refresh, manual_refresh, ats_error))


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


def check_resume(conn: sqlite3.Connection, fields: dict, llm, model: str, now: str) -> str:
    level = (fields.get("level") or (None, b""))[1].decode("utf-8", "replace")
    filename, data = fields.get("resume") or (None, b"")
    jd = (fields.get("jd") or (None, b""))[1].decode("utf-8", "replace")
    if level not in LEVELS:
        return "choose an experience level"
    if not filename:
        return "choose a resume file to upload"
    if len(data) > MAX_UPLOAD_BYTES:
        return "that file is larger than 5 MB"
    try:
        text, pages = extract_text(filename, data)
        result = score_resume(text, pages, level, jd, llm, model)
    except AtsInputError as exc:
        return str(exc)
    except LLMError as exc:
        return f"scoring failed: {exc}"
    record_check(conn, Path(filename).name, level, result, now)
    return ""


def make_server(db_path, host: str, port: int, refresher: Refresher | None = None, applied_command: list[str] = INSTAHYRE_APPLIED_COMMAND, apply_command: list[str] = INSTAHYRE_APPLY_COMMAND, manual_refresher: Refresher | None = None, questionnaires_command: list[str] = QUESTIONNAIRES_COMMAND, llm_factory=None, ats_model: str = "sonnet") -> ThreadingHTTPServer:
    refresher = refresher or Refresher(INSTAHYRE_COMMAND, PROJECT_ROOT)
    manual_refresher = manual_refresher or Refresher(MANUAL_REFRESH_COMMAND, PROJECT_ROOT)
    llm_factory = llm_factory or (lambda conn: LLM(conn, load_settings()["daily_budget_usd"]))
    ats_state = {"error": ""}
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
                if length > MAX_UPLOAD_BYTES + UPLOAD_OVERHEAD_BYTES:
                    ats_state["error"] = "that file is larger than 5 MB"
                    self.close_connection = True
                else:
                    fields = parse_multipart(self.headers.get("Content-Type") or "", self.rfile.read(length))
                    conn = connect(db_path)
                    try:
                        ats_state["error"] = check_resume(conn, fields, llm_factory(conn), ats_model, datetime.now().isoformat(timespec="seconds"))
                    finally:
                        conn.close()
                self.redirect("/?view=ats")
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
            if url.path != "/":
                self.send_error(404)
                return
            view = parse_qs(url.query).get("view", ["today"])[0]
            conn = connect(db_path)
            try:
                error = ats_state["error"] if view == "ats" else ""
                if view == "ats":
                    ats_state["error"] = ""
                body = render(conn, view, date.today(), refresher.status(), manual_refresher.status(), error).encode("utf-8")
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
