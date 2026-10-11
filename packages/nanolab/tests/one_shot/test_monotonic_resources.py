"""Resource phases use bracketing clocks, retaining uncertainty and raw UTC."""

import json
from datetime import UTC, datetime

import pytest

from nanolab.one_shot.vm_resources import summarize_resources
from nanolab.one_shot.vm_sampler import correlate_sample


def probe(host, guest):
    return {
        "node": "edge",
        "beforeMonotonicSeconds": host - 0.001,
        "afterMonotonicSeconds": host + 0.001,
        "remoteMonotonicSeconds": guest,
        "remoteBootId": "boot",
        "errorType": None,
    }


def correlate(raw, *, probes):
    return correlate_sample(
        raw,
        anchor=datetime(2026, 10, 11, tzinfo=UTC),
        warmup=720,
        period=360,
        epochs=9,
        mode="ewma",
        functions=["a"],
        run_id="run",
        host_anchor_monotonic=1000.0,
        clock_probes=probes,
    )


@pytest.mark.parametrize(
    "wall", ["2026-10-11T00:00:01+00:00", "2030-01-01T00:00:01+00:00"]
)
def test_resource_epoch_survives_guest_utc_offset_and_jump(wall):
    raw = {
        "node": "edge",
        "timestampUtc": wall,
        "monotonicSeconds": 71.5,
        "bootId": "boot",
    }
    row = correlate(raw, probes=[probe(1359, 69), probe(1363, 73)])
    assert row["timestampUtc"] == wall
    assert (row["phase"], row["epoch"]) == ("ambiguous", None)
    assert row["epochCandidates"] == [0, 1]
    assert row["timeFromAnchorSeconds"] == pytest.approx(361.5)
    assert row["correlationBoundsSeconds"] == pytest.approx([358.999, 363.001])
    certain = correlate(raw, probes=[probe(1361, 71), probe(1362, 72)])
    assert (certain["phase"], certain["epoch"]) == ("measurement", 1)


@pytest.mark.parametrize("change", ["boot", "missing", "unbracketed"])
def test_resource_correlator_never_guesses_utc_for_missing_or_rebooted_clock(change):
    raw = {
        "node": "edge",
        "timestampUtc": "2026-10-11T00:00:01+00:00",
        "monotonicSeconds": 70,
        "bootId": "boot",
    }
    probes = [probe(1000, 69), probe(1002, 71)]
    if change == "boot":
        raw["bootId"] = "new-boot"
    elif change == "missing":
        probes = []
    else:
        probes = probes[:1]
    row = correlate(raw, probes=probes)
    assert row["phase"] == "uncorrelated"
    assert row["epoch"] is None
    assert row["timeFromAnchorSeconds"] is None


def test_coverage_keeps_raw_and_reports_missing_monotonic_correlations(tmp_path):
    directory = tmp_path / "vm-resources"
    directory.mkdir()
    raw = {
        "node": "edge",
        "timestampUtc": "2030-01-01T00:00:01+00:00",
        "monotonicSeconds": 70.5,
        "bootId": "boot",
        "cpuUtilization": 0.8,
    }
    original = json.dumps(raw) + "\n"
    (directory / "edge.jsonl").write_text(original)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "parameters": {
                    "logicalSchedule": {
                        "protocolVersion": 2,
                        "anchorMonotonicSeconds": 1000,
                    }
                }
            }
        )
    )
    (tmp_path / "clock-probes.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in [probe(1000, 70), probe(1001, 71)])
    )
    result = summarize_resources(
        tmp_path,
        anchor=datetime(2026, 10, 11, tzinfo=UTC),
        warmup=0,
        period=1,
        epochs=1,
        nodes=["edge"],
        mode="ewma",
        functions=["a"],
        run_id="run",
    )
    assert result["nodes"]["edge"]["observedSeconds"] == 1
    assert result["nodes"]["edge"]["uncertainPhaseSamples"] == 1
    assert (directory / "edge.jsonl").read_text() == original
    (tmp_path / "clock-probes.jsonl").unlink()
    missing = summarize_resources(
        tmp_path,
        anchor=datetime(2026, 10, 11, tzinfo=UTC),
        warmup=0,
        period=1,
        epochs=1,
        nodes=["edge"],
        mode="ewma",
        functions=["a"],
        run_id="run",
    )
    assert not missing["complete"]
    assert missing["nodes"]["edge"]["uncorrelatedSamples"] == 1


def test_monotonic_report_plots_on_host_grid_and_keeps_raw_guest_time(tmp_path):
    from nanolab.one_shot.report import render_report
    from tests.one_shot.test_report import manifest

    value = manifest().model_copy(
        update={
            "anchor": "2026-10-11T00:00:00+00:00",
            "nodes": ["edge"],
            "parameters": {
                "vmTelemetry": {},
                "generator": {"warmupSeconds": 0},
                "logicalSchedule": {
                    "protocolVersion": 2,
                    "anchorMonotonicSeconds": 1000,
                },
            },
        }
    )
    (tmp_path / "manifest.json").write_text(value.model_dump_json())
    directory = tmp_path / "vm-resources"
    directory.mkdir()
    raw = {
        "node": "edge",
        "timestampUtc": "2030-01-01T00:00:01+00:00",
        "monotonicSeconds": 70.5,
        "bootId": "boot",
        "cpuUtilization": 0.8,
    }
    (directory / "edge.jsonl").write_text(json.dumps(raw) + "\n")
    (tmp_path / "clock-probes.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in [probe(1000, 70), probe(1001, 71)])
    )
    value.parameters["vmTelemetry"] = {"enabled": True}
    output = render_report(value, None, tmp_path / "report.html").read_text()
    assert "Host monotonic time" in output
    assert "2030-01-01T00:00:01+00:00" in output
    assert "2026-10-11T00:00:00.500000+00:00" in output


def test_archived_raw_summary_retains_its_original_frozen_sampler_policy(tmp_path):
    directory = tmp_path / "vm-resources"
    directory.mkdir()
    frozen = {"schemaVersion": 1, "samplerSha256": "a" * 64, "periodSeconds": 1}
    (directory / "policy.json").write_text(json.dumps(frozen))
    result = summarize_resources(
        tmp_path,
        anchor=datetime(2026, 10, 11, tzinfo=UTC),
        warmup=0,
        period=1,
        epochs=1,
        nodes=["edge"],
        mode="ewma",
        functions=["a"],
        run_id="run",
    )
    assert result["policy"] == frozen


@pytest.mark.parametrize("now", [100.5, 111.0])
def test_owned_sampler_stall_detection_uses_monotonic_write_progress(
    tmp_path, monkeypatch, now
):
    from types import SimpleNamespace

    from nanolab.one_shot.vm_resources import VmTelemetrySession

    path = tmp_path / "edge.jsonl"
    path.write_text("{}\n")
    sampler = VmTelemetrySession.__new__(VmTelemetrySession)
    sampler.sessions = [
        {
            "node": SimpleNamespace(config=SimpleNamespace(id="edge")),
            "output": SimpleNamespace(name=str(path)),
            "process": SimpleNamespace(poll=lambda: None),
            "readerErrors": [],
            "lastWriteMonotonic": 100.0,
        }
    ]
    monkeypatch.setattr(
        "nanolab.one_shot.vm_resources.time.time", lambda: path.stat().st_mtime + 100
    )
    monkeypatch.setattr("nanolab.one_shot.vm_resources.time.monotonic", lambda: now)
    if now > 110:
        with pytest.raises(RuntimeError, match="stalled"):
            sampler.require_healthy()
    else:
        sampler.require_healthy()


@pytest.mark.parametrize("levels", [0, -1, True, 1.5, 1000])
def test_nested_probe_scope_rejects_invalid_ancestor_levels(tmp_path, levels):
    from nanolab.one_shot.vm_resources import resource_probe_paths

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "parameters": {
                    "logicalSchedule": {
                        "protocolVersion": 2,
                        "probeScope": "parent",
                        "probeParentLevels": levels,
                        "anchorMonotonicSeconds": 1000,
                    }
                }
            }
        )
    )
    with pytest.raises(ValueError, match=r"probe.*level"):
        resource_probe_paths(tmp_path)


def test_nested_capacity_scope_uses_original_workflow_probes_and_malformed_records(
    tmp_path,
):
    from nanolab.one_shot.vm_resources import resource_clock_context

    trial = tmp_path / "capacity" / "vector" / "trial"
    trial.mkdir(parents=True)
    (trial / "manifest.json").write_text(
        json.dumps(
            {
                "parameters": {
                    "logicalSchedule": {
                        "protocolVersion": 2,
                        "probeScope": "parent",
                        "probeParentLevels": 3,
                        "anchorMonotonicSeconds": 1000,
                    }
                }
            }
        )
    )
    raw = json.dumps(probe(1000, 70)) + "\n" + "partial-original-probe\n"
    (tmp_path / "clock-probes.jsonl").write_text(raw)
    context = resource_clock_context(trial)
    assert context["clock_probes"] == [probe(1000, 70)]
    result = summarize_resources(
        trial,
        anchor=datetime(2026, 10, 11, tzinfo=UTC),
        warmup=0,
        period=1,
        epochs=1,
        nodes=["edge"],
        mode="capacity",
        functions=["a"],
        run_id="run",
    )
    assert result["clockProbeMalformedLines"] == 1
    assert not result["complete"]
    assert (tmp_path / "clock-probes.jsonl").read_text() == raw
