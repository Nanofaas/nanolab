"""Final VM samples need terminal brackets before coverage becomes immutable."""

import json
from datetime import UTC, datetime

from nanolab.one_shot.vm_resources import summarize_resources
from tests.one_shot.test_monotonic_resources import probe


def prepare_raw(run_dir, *, parent_scope=False):
    directory = run_dir / "vm-resources"
    directory.mkdir(parents=True)
    manifest = {
        "anchor": "2026-10-11T00:00:00+00:00",
        "period_seconds": 2,
        "run_id": "run",
        "parameters": {
            "generator": {"warmupSeconds": 0},
            "logicalSchedule": {
                "protocolVersion": 2,
                "anchorMonotonicSeconds": 1000,
                "probeScope": "parent" if parent_scope else "run",
            },
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    raw = {
        "node": "edge",
        "timestampUtc": manifest["anchor"],
        "bootId": "boot",
        "cpuUtilization": 0.1,
        "memoryTotalBytes": 100,
        "memoryAvailableBytes": 80,
        "memoryUsedBytes": 20,
        "swapTotalBytes": 0,
        "swapFreeBytes": 0,
        "swapUsedBytes": 0,
        "vcpuCount": 1,
        "errors": [],
        "containers": [],
    }
    (directory / "edge.jsonl").write_text(
        "".join(
            json.dumps({**raw, "monotonicSeconds": second}) + "\n"
            for second in (70.5, 71.5)
        )
    )
    probes_path = (run_dir.parent if parent_scope else run_dir) / "clock-probes.jsonl"
    probes_path.write_text(
        "".join(json.dumps(row) + "\n" for row in (probe(1000, 70), probe(1002, 72)))
    )
    return raw, probes_path


def test_malformed_parent_probe_invalidates_parent_scope_coverage(tmp_path):
    run_dir = tmp_path / "load-cell"
    _, probes_path = prepare_raw(run_dir, parent_scope=True)
    with probes_path.open("a") as stream:
        stream.write("{truncated raw probe\n")
    result = summarize_resources(
        run_dir,
        anchor=datetime(2026, 10, 11, tzinfo=UTC),
        warmup=0,
        period=2,
        epochs=1,
        nodes=["edge"],
        mode="ewma",
        functions=["a"],
        run_id="run",
    )
    assert result["clockProbeMalformedLines"] == 1
    assert not result["complete"]
    assert result["nodes"]["edge"]["missingSeconds"] == []
    assert probes_path.read_text().endswith("{truncated raw probe\n")
