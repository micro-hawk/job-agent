import json
import subprocess

NOW = "2026-10-05T09:30:00"


class StubLLM:
    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def call(self, task, model, system, prompt, schema, timeout=180):
        self.calls.append((task, model, prompt))
        result = self.handlers[task](prompt)
        if isinstance(result, Exception):
            raise result
        return result


class FakeRunner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def completed(data=None, cost=0.004, returncode=0, stdout=None, is_error=False, result=None):
    payload = {
        "type": "result",
        "is_error": is_error,
        "result": result if result is not None else (json.dumps(data) if data is not None else ""),
        "structured_output": data,
        "total_cost_usd": cost,
        "usage": {"input_tokens": 3000, "cache_read_input_tokens": 400, "output_tokens": 120},
    }
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=json.dumps(payload) if stdout is None else stdout, stderr="")
