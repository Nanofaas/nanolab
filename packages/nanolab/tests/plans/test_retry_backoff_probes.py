import importlib.util
import json
import sys
from pathlib import Path

import pytest

ASSETS = Path(__file__).parents[2] / "src/nanolab/assets/validation"


@pytest.fixture
def probes(monkeypatch):
    monkeypatch.syspath_prepend(str(ASSETS))
    modules = []
    for name in ("retry_backoff_burst", "retry_hint_probe"):
        spec = importlib.util.spec_from_file_location(name, ASSETS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


def successful_responses():
    return [
        (
            200,
            {},
            json.dumps({"executionId": str(i), "status": "success", "error": None}),
        )
        for i in range(300)
    ]


def test_managed_candidate_requires_success_and_distinct_admissions(probes):
    burst, _ = probes
    rows = successful_responses()
    assert len(burst.validate_successes(rows)) == 300
    for bad in [
        (200, {}, '{"status":"error","error":{"code":"EXTERNAL_ERROR"}}'),
        (429, {}, "{}"),
        rows[1],
    ]:
        with pytest.raises(RuntimeError):
            burst.validate_successes([bad, *rows[1:]])
    with pytest.raises(RuntimeError):
        burst.validate_successes(rows[:-1])


def test_hint_candidate_rejects_early_duplicate_missing_or_wrong_attempts(probes):
    _, hint = probes
    calls = [
        {"sequence": i, "response": response}
        for i, response in enumerate(successful_responses())
    ]
    attempts = [
        {
            "executionId": str(i),
            "attempt": str(n),
            "idempotencyKey": f"hint-{i}",
            "upstreamReceivedMonotonicNanos": (n - 1) * 1_000_000_000,
            "outcome": 429 if n == 1 else 200,
        }
        for i in range(300)
        for n in (1, 2)
    ]
    hint.validate_candidate(calls, attempts)
    for change in (
        {"upstreamReceivedMonotonicNanos": 999_999_999},
        {"attempt": "1"},
        {"executionId": "unknown"},
        {"outcome": 429},
    ):
        changed = [dict(row) for row in attempts]
        changed[1].update(change)
        with pytest.raises(RuntimeError):
            hint.validate_candidate(calls, changed)
    with pytest.raises(RuntimeError):
        hint.validate_candidate(calls, attempts[:-1])
    calls[0]["response"] = (200, {}, '{"status":"error"}')
    with pytest.raises(RuntimeError):
        hint.validate_candidate(calls, attempts)
