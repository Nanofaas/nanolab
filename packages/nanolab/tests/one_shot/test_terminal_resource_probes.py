"""Final VM samples need terminal brackets before coverage becomes immutable."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nanolab.one_shot.vm_resources import VmTelemetrySession, summarize_resources
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


@pytest.mark.parametrize("terminal_fails", [False, True])
def test_close_brackets_drained_tail_before_freezing_coverage(tmp_path, terminal_fails):
    raw, probes_path = prepare_raw(tmp_path)
    actions = []
    session = VmTelemetrySession.__new__(VmTelemetrySession)
    session.closed = False
    session.run = SimpleNamespace(
        run_dir=tmp_path,
        topology=SimpleNamespace(resources=SimpleNamespace(nodes={"edge": None})),
        trace=SimpleNamespace(windows=[{}]),
        settings=SimpleNamespace(functions={"a": {}}),
        mode="ewma",
    )
    output = (tmp_path / "vm-resources/edge.jsonl").open("ab", buffering=0)
    errors = (tmp_path / "vm-resources/edge.stderr").open("wb", buffering=0)

    def drain_tail(**_kwargs):
        actions.append("drain")
        output.write((json.dumps({**raw, "monotonicSeconds": 72.5}) + "\n").encode())

    session.sessions = [
        {
            "node": SimpleNamespace(command=lambda _: actions.append("stop")),
            "remote": "owned-sampler.py",
            "process": SimpleNamespace(wait=lambda **_: actions.append("reap")),
            "readers": [SimpleNamespace(join=drain_tail, is_alive=lambda: False)],
            "readerErrors": [],
            "output": output,
            "errors": errors,
        }
    ]

    def terminal_probe():
        assert output.closed and errors.closed
        assert actions == ["stop", "reap", "drain"]
        actions.append("terminal-probe")
        with probes_path.open("a") as stream:
            row = (
                {"node": "edge", "errorType": "ConnectError"}
                if terminal_fails
                else probe(1003, 73)
            )
            stream.write(json.dumps(row) + "\n")

    session.terminal_probe = terminal_probe
    session.close()
    assert actions[-1] == "terminal-probe"
    coverage = json.loads((tmp_path / "vm-resources/coverage.json").read_text())
    assert coverage["complete"] is not terminal_fails
    assert coverage["nodes"]["edge"]["missingSeconds"] == []
    assert coverage["nodes"]["edge"]["uncorrelatedSamples"] == int(terminal_fails)
    assert len((tmp_path / "vm-resources/edge.jsonl").read_text().splitlines()) == 3


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
