import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "pilot",
    str(Path(__file__).resolve().parents[4] / "scripts/one-shot-logging-overhead.py"),
)
assert spec is not None and spec.loader is not None
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def test_known_checksum_ignores_invocation_identity():
    a = {"executionId": "first", "status": "success", "output": 18446744073709551615}
    b = {**a, "executionId": "second"}
    assert (
        pilot.response_checksum(json.dumps(a))
        == pilot.response_checksum(json.dumps(b))
        == "18446744073709551615"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"status": "ERROR", "output": 1},
        {"status": "success", "output": True},
        {"status": "success", "output": None},
    ],
)
def test_invalid_outcome_not_counted(body):
    with pytest.raises(ValueError, match="successful u64 checksum"):
        pilot.response_checksum(json.dumps(body))


def test_boundary_extra_kept_but_failed_missing_or_dropped_fail_gate():
    good = {
        "planned": 2,
        "sent": 3,
        "completed": 3,
        "dropped": 0,
        "generatorDeficit": 0,
    }
    assert pilot.accounting_valid(good)
    for bad in [
        {**good, "completed": 2},
        {**good, "dropped": 1},
        {**good, "sent": 1, "completed": 1, "generatorDeficit": 1},
    ]:
        assert not pilot.accounting_valid(bad)


def test_resource_cost_uses_cumulative_counters_not_post_load_cpu_percent():
    before = {
        "workload": {"cpuNs": 100, "readBytes": 10, "writeBytes": 20, "memoryBytes": 40}
    }
    after = {
        "workload": {"cpuNs": 900, "readBytes": 14, "writeBytes": 30, "memoryBytes": 80}
    }
    delta = pilot.resource_delta(before, after, elapsed_seconds=2)
    assert delta["workload"]["cpuNs"] == 800
    assert delta["workload"]["readBytes"] == 4
    assert delta["workload"]["writeBytes"] == 10
    assert delta["workload"]["meanCores"] == 800 / 2e9
    assert delta["workload"]["memoryEndBytes"] == 80
    with pytest.raises(ValueError, match="counter"):
        pilot.resource_delta(after, before, elapsed_seconds=2)
