import json
import subprocess
from datetime import datetime

import pytest

from agent.llm import LLM, BudgetExceeded, LLMError

from tests.fakes import FakeRunner, completed

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def clock():
    return datetime(2026, 10, 5, 9, 30)


def make(conn, *responses, budget=3.0):
    runner = FakeRunner(*responses)
    return LLM(conn, budget, runner=runner, clock=clock), runner


def logged(conn):
    return [dict(row) for row in conn.execute("SELECT task, model, input_tokens, output_tokens, cost_usd, ok, error FROM llm_calls")]


def test_call_builds_cheap_command_and_logs_usage(conn):
    llm, runner = make(conn, completed({"ok": True}, cost=0.0041))
    assert llm.call("extract", "haiku", "SYS", "PROMPT", SCHEMA) == {"ok": True}
    cmd, kwargs = runner.calls[0]
    assert cmd[:4] == ["claude", "-p", "--model", "haiku"]
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--no-session-persistence" in cmd
    assert kwargs["input"] == "PROMPT"
    assert logged(conn) == [{"task": "extract", "model": "haiku", "input_tokens": 3400, "output_tokens": 120, "cost_usd": 0.0041, "ok": 1, "error": ""}]


def test_falls_back_to_result_string(conn):
    response = completed(None, result='{"ok": false}')
    llm, _ = make(conn, response)
    assert llm.call("extract", "haiku", "S", "P", SCHEMA) == {"ok": False}


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (completed({"ok": True}, returncode=1, is_error=True, result="rate limited"), "rate limited"),
        (subprocess.CompletedProcess(args=[], returncode=1, stdout="Error: not logged in", stderr=""), "non-JSON"),
        (completed(None, result="sure! here you go"), "no structured output"),
        (subprocess.TimeoutExpired(cmd="claude", timeout=180), "timeout"),
        (FileNotFoundError("claude"), "cannot run"),
        (subprocess.CompletedProcess(args=[], returncode=0, stdout='[{"type": "result"}]', stderr=""), "non-object"),
        (subprocess.CompletedProcess(args=[], returncode=0, stdout="null", stderr=""), "non-object"),
    ],
)
def test_failures_raise_llm_error_and_are_logged(conn, response, fragment):
    llm, _ = make(conn, response)
    with pytest.raises(LLMError) as info:
        llm.call("score", "sonnet", "S", "P", SCHEMA)
    assert fragment in str(info.value)
    assert logged(conn)[0]["ok"] == 0


def test_budget_blocks_calls_once_reached(conn):
    llm, runner = make(conn, completed({"ok": True}, cost=0.6), completed({"ok": True}, cost=0.6), budget=1.0)
    llm.call("score", "sonnet", "S", "P", SCHEMA)
    llm.call("score", "sonnet", "S", "P", SCHEMA)
    assert llm.spent_today() == pytest.approx(1.2)
    with pytest.raises(BudgetExceeded):
        llm.call("score", "sonnet", "S", "P", SCHEMA)
    assert len(runner.calls) == 2


def test_spend_from_other_days_is_ignored(conn):
    conn.execute("INSERT INTO llm_calls (ts, task, model, cost_usd, ok) VALUES ('2026-10-04T23:59:00', 'score', 'sonnet', 99, 1)")
    llm, _ = make(conn, completed({"ok": True}))
    assert llm.call("score", "sonnet", "S", "P", SCHEMA) == {"ok": True}
