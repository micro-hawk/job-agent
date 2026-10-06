import json
import sqlite3
import subprocess
from datetime import datetime


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


class LLM:
    def __init__(self, conn: sqlite3.Connection, budget_usd: float, runner=subprocess.run, binary: str = "claude", clock=datetime.now):
        self.conn = conn
        self.budget_usd = budget_usd
        self.runner = runner
        self.binary = binary
        self.clock = clock

    def spent_today(self) -> float:
        day = self.clock().date().isoformat()
        row = self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM llm_calls WHERE substr(ts, 1, 10)=?", (day,)).fetchone()
        return float(row["spent"])

    def call(self, task: str, model: str, system: str, prompt: str, schema: dict, timeout: int = 180) -> dict:
        if self.spent_today() >= self.budget_usd:
            raise BudgetExceeded(f"daily LLM budget ${self.budget_usd:.2f} reached")
        command = [
            self.binary, "-p", "--model", model, "--output-format", "json", "--system-prompt", system,
            "--json-schema", json.dumps(schema), "--tools", "", "--no-session-persistence", "--setting-sources", "",
        ]
        try:
            process = self.runner(command, input=prompt, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return self._fail(task, model, {}, f"timeout after {timeout}s")
        except OSError as exc:
            return self._fail(task, model, {}, f"cannot run {self.binary}: {exc.__class__.__name__}")
        try:
            output = json.loads(process.stdout)
        except json.JSONDecodeError:
            return self._fail(task, model, {}, f"exit {process.returncode}: non-JSON output")
        if not isinstance(output, dict):
            return self._fail(task, model, {}, f"exit {process.returncode}: non-object JSON output")
        if process.returncode != 0 or output.get("is_error"):
            return self._fail(task, model, output, str(output.get("result") or process.stderr or "error")[:200])
        data = output.get("structured_output")
        if data is None:
            try:
                data = json.loads(output.get("result") or "")
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict):
            return self._fail(task, model, output, "no structured output")
        self._log(task, model, output, True, "")
        return data

    def _fail(self, task: str, model: str, output: dict, error: str):
        self._log(task, model, output, False, error)
        raise LLMError(f"{task}/{model}: {error}")

    def _log(self, task: str, model: str, output: dict, ok: bool, error: str) -> None:
        usage = output.get("usage") or {}
        input_tokens = sum(int(usage.get(key) or 0) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        self.conn.execute(
            "INSERT INTO llm_calls (ts, task, model, input_tokens, output_tokens, cost_usd, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.clock().isoformat(timespec="seconds"), task, model, input_tokens, int(usage.get("output_tokens") or 0),
                float(output.get("total_cost_usd") or 0), int(ok), error,
            ),
        )
        self.conn.commit()
