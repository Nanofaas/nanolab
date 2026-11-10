"""scripts/compare-run-shape.py on small fixture run directories."""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[4] / "scripts/compare-run-shape.py"
spec = importlib.util.spec_from_file_location("compare_run_shape", SCRIPT)
assert spec is not None and spec.loader is not None
shape = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shape)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def run(root: Path, *, row_extra: dict | None = None, metrics: dict | None = None):
    """Write a one-node run with every facet the comparison reads."""
    write_jsonl(
        root / "metrics.jsonl",
        [
            {
                "at": "2026-11-10T00:00:00+00:00",
                "node": "edge-1",
                "function": "f",
                "replica": 1,
                "metrics": metrics
                or {"nanofaas_runtime_active_handlers": 1, "queue_depth": 3},
            }
        ],
    )
    write_jsonl(
        root / "logging/edge-1/queue-events.jsonl",
        [{"attributes": {"event": "QUEUED"}}, {"attributes": {"event": "DISPATCH"}}],
    )
    row = {
        "timestampUtc": "2026-11-10T00:00:00+00:00",
        "node": "edge-1",
        "memoryUsedBytes": 600_000_000,
        "containers": [{"containerId": "a", "role": "function"}],
        "sampler": {"cpuUsedCores": 0.01},
        **(row_extra or {}),
    }
    write_jsonl(root / "vm-resources/edge-1.jsonl", [row])
    (root / "vm-resources/coverage.json").write_text(
        json.dumps({"complete": True, "nodes": {"edge-1": {"missingSeconds": []}}})
    )
    write_jsonl(
        root / "supervision.jsonl",
        [{"kind": "heartbeat"}, {"kind": "campaign.started"}],
    )
    return root


def test_same_shape_passes(tmp_path):
    reference = run(tmp_path / "old")
    new = run(tmp_path / "new")
    # Values may differ within an order of magnitude.
    rows = new / "vm-resources/edge-1.jsonl"
    rows.write_text(rows.read_text().replace("600000000", "900000000"))
    assert shape.compare(reference, new) == []


def test_missing_series_reported(tmp_path):
    reference = run(tmp_path / "old")
    new = run(tmp_path / "new", metrics={"nanofaas_runtime_active_handlers": 1})
    assert shape.compare(reference, new) == [
        "metrics.jsonl: metrics series: missing in NEW: queue_depth"
    ]


def test_extra_row_key_reported(tmp_path):
    reference = run(tmp_path / "old")
    new = run(tmp_path / "new", row_extra={"surprise": 1})
    assert shape.compare(reference, new) == [
        "vm-resources/edge-1.jsonl: vm-resources row keys: extra in NEW: surprise"
    ]


def test_sampler_only_keys_and_implausible_values(tmp_path):
    reference = run(tmp_path / "old", row_extra={"bootId": "b", "monotonicSeconds": 1})
    new = run(tmp_path / "new")
    assert shape.compare(reference, new) == []
    rows = new / "vm-resources/edge-1.jsonl"
    rows.write_text(rows.read_text().replace("600000000", "6"))
    assert shape.compare(reference, new) == [
        "vm-resources/edge-1.jsonl median memoryUsedBytes: 6e+08 vs 6: "
        "not the same order of magnitude"
    ]
    rows.write_text("")
    assert "vm-resources/edge-1.jsonl: empty in NEW" in shape.compare(reference, new)


def test_a_report_that_does_not_regenerate_is_reported(tmp_path):
    new = run(tmp_path / "new")
    (new / "manifest.json").write_text("{}")
    (new / "report.json").write_text("{}")
    (failure,) = shape.regenerate(new)
    assert failure.startswith("report.json does not regenerate in .:")


def test_loki_kind_label_and_coverage_key_differences(tmp_path):
    reference = run(tmp_path / "old")
    new = run(tmp_path / "new")
    write_jsonl(
        new / "logging/edge-1/queue-events.jsonl", [{"attributes": {"event": "QUEUED"}}]
    )
    rows = new / "metrics.jsonl"
    rows.write_text(rows.read_text().replace('"replica"', '"pod"'))
    (new / "vm-resources/coverage.json").write_text(
        json.dumps({"complete": True, "nodes": {"edge-1": {}}})
    )
    assert shape.compare(reference, new) == [
        "logging/edge-1/queue-events.jsonl: Loki event kinds: missing in NEW: DISPATCH",
        "metrics.jsonl: metrics labels: missing in NEW: replica",
        "metrics.jsonl: metrics labels: extra in NEW: pod",
        "vm-resources/coverage.json: coverage keys: missing in NEW: nodes.edge-1.missingSeconds",
    ]


def test_fault_kinds_come_from_anomalies_and_failure_causes(tmp_path):
    reference = run(tmp_path / "old")
    with (reference / "supervision.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "kind": "operational.failure",
                    "cause": "edge-1: required service stopped: ['collector']",
                }
            )
            + "\n"
        )
    new = run(tmp_path / "new")
    (new / "workbench").mkdir()
    (new / "workbench/supervisor-status.json").write_text(
        json.dumps({"anomalies": [{"target": "edge-1", "kind": "scrape_up"}]})
    )
    assert shape.compare(reference, new) == [
        "workbench/supervisor-status.json: extra file in NEW",
        "supervision.jsonl: supervisor fault kinds: missing in NEW: required service stopped",
    ]


def test_nodes_missing_in_new_are_reported(tmp_path):
    reference = run(tmp_path / "old")
    write_jsonl(reference / "vm-resources/cloud.jsonl", [{"memoryUsedBytes": 1}])
    (reference / "vm-resources/coverage.json").write_text(
        json.dumps(
            {
                "complete": True,
                "nodes": {
                    "edge-1": {"missingSeconds": []},
                    "cloud": {"missingSeconds": []},
                },
            }
        )
    )
    new = run(tmp_path / "new")
    assert shape.compare(reference, new) == [
        "vm-resources/cloud.jsonl: file missing in NEW",
        "vm-resources/coverage.json: coverage keys: missing in NEW: nodes.cloud.missingSeconds",
        "vm-resources/coverage.json cloud: node missing in NEW",
    ]


@pytest.mark.parametrize(
    "damage", ["missing-trial", "empty-metrics", "memory", "coverage", "schema"]
)
def test_nested_trials_do_not_cover_each_others_missing_evidence(tmp_path, damage):
    old, new = tmp_path / "old", tmp_path / "new"
    for root in (old, new):
        for trial in ("trial-a", "trial-b"):
            run(root / trial)
    target = new / "trial-b"
    if damage == "missing-trial":
        shutil.rmtree(target)
    elif damage == "empty-metrics":
        (target / "metrics.jsonl").write_text("")
    elif damage == "memory":
        # A global median across trials used to hide this impossible value.
        path = target / "vm-resources/edge-1.jsonl"
        path.write_text(path.read_text().replace("600000000", "6"))
    elif damage == "coverage":
        (target / "vm-resources/coverage.json").write_text(
            json.dumps({"complete": True, "nodes": {}})
        )
    else:
        path = target / "metrics.jsonl"
        path.write_text(path.read_text().replace('"queue_depth": 3', '"different": 3'))
        # Both runs have the same global union, but opposite schema per trial.
        path = old / "trial-a/metrics.jsonl"
        path.write_text(path.read_text().replace('"queue_depth": 3', '"different": 3'))
    differences = shape.compare(old, new)
    assert differences
    assert any("trial-b/" in message for message in differences)


def test_identical_nested_trials_pass(tmp_path):
    for root in (tmp_path / "old", tmp_path / "new"):
        run(root / "capacity/trial-a")
        run(root / "campaign/trial-b")
    assert shape.compare(tmp_path / "old", tmp_path / "new") == []
