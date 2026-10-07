import json
from datetime import datetime
from pathlib import Path

import httpx

import agent.run as run_module
from agent.config import EXAMPLE_DIR, load_settings
from agent.run import main, run_pipeline

from tests.fakes import StubLLM

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = load_settings(EXAMPLE_DIR)
SUMMARY = {"stack": ["Java"], "must_haves": [], "min_years": 3, "seniority": "senior", "sponsorship": "unknown", "salary": "", "location": "Bengaluru", "red_flags": []}


def test_stop_file_exits_before_any_work(tmp_path, monkeypatch, capsys):
    stop = tmp_path / "STOP"
    stop.touch()
    db_path = tmp_path / "data" / "agent.db"
    monkeypatch.setattr(run_module, "STOP_FILE", stop)
    monkeypatch.setattr(run_module, "DB_PATH", db_path)
    assert main(["run"]) == 0
    assert "STOP" in capsys.readouterr().out
    assert not db_path.exists()


def test_pipeline_runs_end_to_end_without_gmail_password(conn):
    payload = json.loads((FIXTURES / "greenhouse.json").read_text())
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    llm = StubLLM({
        "extract": lambda prompt: SUMMARY,
        "prescore": lambda prompt: {"scores": [{"id": 1, "score": 82}]},
        "score": lambda prompt: {"score": 86, "reasons": ["Java + Kafka"], "gaps": []},
    })
    companies = [{"name": "Acme", "platform": "greenhouse", "token": "acme", "tier": "A"}]
    stats, errors = run_pipeline(conn, SETTINGS, companies, "brief", llm, client, None, datetime(2026, 10, 5, 9, 30))
    assert errors == ["gmail: GMAIL_APP_PASSWORD not set; alert emails skipped"]
    assert stats["ats"] == {"fetched": 2, "new": 2}
    assert stats["filter"] == {"candidate": 1, "manual": 0, "filtered_out": 1}
    assert stats["score"]["ready"] == 1
    rows = {row["title"]: row["status"] for row in conn.execute("SELECT title, status FROM jobs")}
    assert rows == {"Senior Software Engineer": "ready", "Product Designer": "filtered_out"}


def test_pipeline_skip_flags(conn):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"jobs": []})))
    llm = StubLLM({})
    stats, errors = run_pipeline(conn, SETTINGS, [], "brief", llm, client, None, datetime(2026, 10, 5), skip_email=True, skip_score=True)
    assert errors == []
    assert "score" not in stats and "alerts" not in stats
    assert llm.calls == []


def test_manual_refresh_reads_confirmations_and_prints_the_result(conn, capsys):
    from datetime import date
    from agent.run import manual_refresh_command
    seen = {}

    def fetch(user, password, since):
        seen.update(user=user, since=since)
        return []

    assert manual_refresh_command(conn, SETTINGS, "secret", date(2026, 10, 5), fetch) == 0
    assert seen == {"user": SETTINGS["gmail"]["user"], "since": date(2026, 9, 14)}
    assert json.loads(capsys.readouterr().out) == {"linkedin_applied": 0, "naukri_applied": 0, "expired": 0}


def test_manual_refresh_without_gmail_password_says_so(conn, capsys):
    from datetime import date
    from agent.run import manual_refresh_command
    assert manual_refresh_command(conn, SETTINGS, "", date(2026, 10, 5), lambda *a: []) == 1
    assert "GMAIL_APP_PASSWORD" in capsys.readouterr().out
