"""scripts/compare-run-shape.py on small fixture run directories."""

import importlib.util
import json
from pathlib import Path

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
        "metrics series: missing in NEW: queue_depth"
    ]


def test_extra_row_key_reported(tmp_path):
    reference = run(tmp_path / "old")
    new = run(tmp_path / "new", row_extra={"surprise": 1})
    assert shape.compare(reference, new) == [
        "vm-resources row keys: extra in NEW: surprise"
    ]


def test_sampler_only_keys_and_implausible_values(tmp_path):
    reference = run(tmp_path / "old", row_extra={"bootId": "b", "monotonicSeconds": 1})
    new = run(tmp_path / "new")
    assert shape.compare(reference, new) == []
    rows = new / "vm-resources/edge-1.jsonl"
    rows.write_text(rows.read_text().replace("600000000", "6"))
    assert shape.compare(reference, new) == [
        "vm-resources edge-1 median memoryUsedBytes: 6e+08 vs 6: "
        "not the same order of magnitude"
    ]
    rows.write_text("")
    assert "vm-resources edge-1: empty in NEW" in shape.compare(reference, new)


def test_a_report_that_does_not_regenerate_is_reported(tmp_path):
    new = run(tmp_path / "new")
    (new / "manifest.json").write_text("{}")
    (new / "report.json").write_text("{}")
    (failure,) = shape.regenerate(new)
    assert failure.startswith("report.json does not regenerate in .:")
