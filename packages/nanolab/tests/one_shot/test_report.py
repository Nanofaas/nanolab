import json
from typing import Literal

import pytest

from nanolab.one_shot.models import CampaignManifest
from nanolab.one_shot.report import render_campaign, render_report
from nanolab.tasks.one_shot.conservation import RunEvidence

MODES: tuple[Literal["baseline", "oracle", "ewma"], ...] = (
    "oracle",
    "baseline",
    "ewma",
)


def manifest(mode: Literal["baseline", "oracle", "ewma"] = "oracle"):
    return CampaignManifest(
        provider="multipass",
        purpose="workflow-validation",
        environment_fingerprint="fp",
        source_commit="a" * 40,
        profile_sha256="b" * 64,
        qualification_sha256="c" * 64,
        trace_sha256="d" * 64,
        seed=7,
        nodes=["edge-0", "edge-1", "cloud"],
        functions=["work"],
        period_seconds=20,
        lead_seconds=2,
        flow_quantum=1,
        modes=list(MODES),
        mode=mode,
        parameters={"generator": {"repetitions": 1}},
    )


def evidence(*, valid=True):
    return RunEvidence(
        planned=10,
        emitted=10,
        received=10,
        attempts=10,
        completed=10 if valid else 8,
        errors=0,
        censored=0 if valid else 2,
        generator_deficit=0,
        valid=valid,
        violations=[] if valid else ["missing physical proof"],
        cells=[
            {
                "origin": "edge-0",
                "function": "work",
                "epoch": 0,
                "planned": 10,
                "completed": 10 if valid else 8,
                "errors": 0,
                "destinations": {"edge-0": 6, "edge-1": 2, "cloud": 2},
            }
        ],
        observations={
            "plannedWindows": [{}],
            "loadExecuted": True,
            "latenciesSeconds": [0.1, 0.2],
            "realizedUtility": 0.6 if valid else None,
        },
    )


def test_missing_and_censored_evidence_never_produces_valid_comparison(tmp_path):
    for name, value in [("missing", None), ("censored", evidence(valid=False))]:
        output = tmp_path / (name + ".html")
        assert render_report(manifest(), value, output) == output
        summary = json.loads(output.with_suffix(".json").read_text())
        assert not summary["conforming"]
        assert summary["provider"] == "multipass"
        assert "workflow-validation" in output.read_text()
        if value is not None:
            assert summary["censored"] == 2


def test_numerical_report_and_plot_share_measured_counts(tmp_path):
    path = render_report(manifest(), evidence(), tmp_path / "run.html")
    data = json.loads(path.with_suffix(".json").read_text())
    assert data["conforming"]
    assert data["throughputOriginalsPerSecond"] == 0.5
    assert data["routing"] == {"local": 6, "peer": 2, "cloud": 2}
    assert data["realizedUtility"] == 0.6
    assert data["telemetryUnavailable"]
    assert data["latencySeconds"]["p95"] == 0.195


def test_campaign_requires_all_modes_and_reports_uncertainty_as_unavailable_for_n_one(
    tmp_path,
):
    for mode in MODES:
        directory = tmp_path / ("rep-0-" + mode)
        directory.mkdir()
        (directory / "manifest.json").write_text(manifest(mode).model_dump_json())
        (directory / "evidence.json").write_text(evidence().model_dump_json())
    path = render_campaign(tmp_path, workflow_completed=True)
    data = json.loads(path.with_suffix(".json").read_text())
    assert data["validComparison"]
    assert all(
        row["confidence95"] is None for row in data["betweenRepetitions"].values()
    )
    (tmp_path / "rep-0-ewma/evidence.json").unlink()
    render_campaign(tmp_path, workflow_completed=True)
    assert not json.loads(path.with_suffix(".json").read_text())["validComparison"]


def test_zero_load_cannot_be_resurrected_by_valid_flag(tmp_path):
    value = evidence().model_copy(update={"planned": 0, "emitted": 0, "completed": 0})
    path = render_report(manifest(), value, tmp_path / "empty.html")
    assert not json.loads(path.with_suffix(".json").read_text())["conforming"]


def test_distinct_run_anchors_are_allowed_but_duplicate_modes_are_not(tmp_path):
    for index, mode in enumerate(MODES):
        directory = tmp_path / ("rep-0-" + mode)
        directory.mkdir()
        value = manifest(mode).model_copy(update={"anchor": str(index), "run_id": mode})
        (directory / "manifest.json").write_text(value.model_dump_json())
        (directory / "evidence.json").write_text(evidence().model_dump_json())
    path = render_campaign(tmp_path, workflow_completed=True)
    assert json.loads(path.with_suffix(".json").read_text())["validComparison"]
    (tmp_path / "rep-0-ewma/manifest.json").write_text(
        manifest("baseline").model_dump_json()
    )
    render_campaign(tmp_path, workflow_completed=True)
    assert not json.loads(path.with_suffix(".json").read_text())["validComparison"]


def test_changed_runtime_package_invalidates_comparison(tmp_path):
    for mode in MODES:
        directory = tmp_path / ("rep-0-" + mode)
        directory.mkdir()
        value = manifest(mode)
        value.parameters["nanolabPackageSha256"] = mode
        (directory / "manifest.json").write_text(value.model_dump_json())
        (directory / "evidence.json").write_text(evidence().model_dump_json())
    path = render_campaign(tmp_path, workflow_completed=True)
    assert not json.loads(path.with_suffix(".json").read_text())["validComparison"]


def test_report_exposes_native_wall_ratio_transitions_and_observed_queues(tmp_path):
    value = evidence()
    value.observations.update(
        {
            "epochs": [
                {
                    "epoch": 0,
                    "auctionWallUpperBoundSeconds": 0.1,
                    "globalReadyWallSeconds": 0.2,
                    "censored": False,
                    "nodes": {
                        "edge-0": {
                            "result": {
                                "desiredReplicas": {"work": 2},
                                "readyReplicas": {"work": 1},
                            }
                        }
                    },
                }
            ],
            "metricSamples": [
                {
                    "at": "sample",
                    "node": "cloud",
                    "metrics": {"executor_queued_tasks": 3.0, "process_cpu_usage": 0.5},
                }
            ],
        }
    )
    path = render_report(manifest(), value, tmp_path / "native.html")
    summary = json.loads(path.with_suffix(".json").read_text())
    assert summary["auctionEpochs"][0]["auctionToPeriodRatio"] == 0.005
    assert summary["transitions"][0]["desiredReplicas"] == {"work": 2}
    assert (
        summary["telemetry"]["metricSamples"][0]["metrics"]["executor_queued_tasks"]
        == 3.0
    )
    assert "executor_queued_tasks" in path.read_text()


def test_between_repetition_interval_requires_independent_complete_runs(tmp_path):
    for rep in range(2):
        for mode in MODES:
            directory = tmp_path / f"rep-{rep}-{mode}"
            directory.mkdir()
            frozen = manifest(mode).model_copy(update={"repetition": rep})
            frozen.parameters["generator"] = {"repetitions": 2}
            (directory / "manifest.json").write_text(frozen.model_dump_json())
            (directory / "evidence.json").write_text(evidence().model_dump_json())
    path = render_campaign(tmp_path, workflow_completed=True)
    summary = json.loads(path.with_suffix(".json").read_text())
    assert summary["validComparison"]
    assert summary["betweenRepetitions"]["oracle"]["confidence95"] == [0.5, 0.5]


def test_censored_native_epoch_without_result_retains_unavailable_transition(tmp_path):
    value = evidence(valid=False)
    value.observations["epochs"] = [
        {"epoch": 0, "censored": True, "nodes": {"edge-0": {"result": None}}}
    ]
    path = render_report(manifest(), value, tmp_path / "censored-native.html")
    summary = json.loads(path.with_suffix(".json").read_text())
    assert not summary["conforming"]
    assert summary["auctionEpochs"][0]["auctionToPeriodRatio"] is None
    assert summary["transitions"][0]["readyReplicas"] is None


def test_a_single_declared_mode_cannot_qualify_the_three_mode_comparison(tmp_path):
    directory = tmp_path / "rep-0-baseline"
    directory.mkdir()
    frozen = manifest("baseline").model_copy(update={"modes": ["baseline"]})
    (directory / "manifest.json").write_text(frozen.model_dump_json())
    (directory / "evidence.json").write_text(evidence().model_dump_json())
    path = render_campaign(tmp_path, workflow_completed=True)
    assert not json.loads(path.with_suffix(".json").read_text())["validComparison"]


def test_failed_workflow_cannot_publish_valid_comparison(tmp_path):
    for mode in MODES:
        directory = tmp_path / ("rep-0-" + mode)
        directory.mkdir()
        (directory / "manifest.json").write_text(manifest(mode).model_dump_json())
        (directory / "evidence.json").write_text(evidence().model_dump_json())
    (tmp_path / "run-metadata.json").write_text(json.dumps({"status": "failed"}))
    path = render_campaign(tmp_path, workflow_completed=True)
    data = json.loads(path.with_suffix(".json").read_text())
    assert not data["validComparison"]
    assert all(row["conforming"] for row in data["runs"])


def test_missing_workflow_outcome_is_not_inferred_from_physical_measurements(tmp_path):
    for mode in MODES:
        directory = tmp_path / ("rep-0-" + mode)
        directory.mkdir()
        (directory / "manifest.json").write_text(manifest(mode).model_dump_json())
        (directory / "evidence.json").write_text(evidence().model_dump_json())
    path = render_campaign(tmp_path)
    assert not json.loads(path.with_suffix(".json").read_text())["validComparison"]


def test_cloud_direct_counts_are_separate_from_edge_offload_and_goodput():
    from nanolab.one_shot.report import summarize

    value = evidence()
    value.cells.append(
        {
            "origin": "cloud",
            "function": "work",
            "epoch": 0,
            "planned": 100,
            "completed": 100,
            "errors": 0,
            "destinations": {"cloud": 100},
        }
    )
    value.planned += 100
    value.emitted += 100
    value.received += 100
    value.attempts += 100
    value.completed += 100
    summary = summarize(manifest(), value)
    assert summary["routing"] == {"local": 6, "peer": 2, "cloud": 2}
    assert summary["throughputOriginalsPerSecond"] == 0.5
    assert summary["cloudDirect"] == {"planned": 100, "completed": 100, "errors": 0}
    assert summary["platformThroughputOriginalsPerSecond"] == 5.5


def test_archived_vm_raw_enables_charts_without_manifest_flag(tmp_path, monkeypatch):
    raw = tmp_path / "vm-resources"
    raw.mkdir()
    (raw / "edge-0.jsonl").write_text("{}\n")
    observed = []
    monkeypatch.setattr(
        "nanolab.one_shot.report.resource_figures",
        lambda *args: observed.append(True) or [],
    )
    monkeypatch.setattr(
        "nanolab.one_shot.vm_resources.summarize_resources",
        lambda *args, **kwargs: {"complete": False},
    )
    render_report(
        manifest().model_copy(update={"anchor": "2026-10-13T00:00:00+00:00"}),
        None,
        tmp_path / "report.html",
    )
    assert observed == [True]


def test_assignment_report_retains_valid_rows_before_truncated_tail(tmp_path):
    from nanolab.one_shot.report import assignment_observations

    complete = {
        "epoch": 0,
        "nodes": {
            "edge": {
                "result": {
                    "status": "DEGRADED",
                    "plan": {"functions": {"f": {"cloudRate": 1}}},
                    "diagnostics": [],
                }
            }
        },
    }
    (tmp_path / "epochs.jsonl").write_text(json.dumps(complete) + '\n{"epoch":')
    (tmp_path / "assignment-observations.jsonl").write_text(
        '{"kind":"unusedReservation"}\n{"kind":'
    )
    summary = assignment_observations(tmp_path)
    assert len(summary["cells"]) == 1
    assert len(summary["unusedReservations"]) == 1
    assert summary["malformedLines"] == {
        "epochs.jsonl": 1,
        "assignment-observations.jsonl": 1,
    }
    assert (tmp_path / "epochs.jsonl").read_text().endswith('{"epoch":')


def embedded_summary(document):
    import html
    import re

    summaries = re.findall(r"<pre>(.*?)</pre>", document, re.S)
    assert len(summaries) == 1
    return json.loads(html.unescape(summaries[0]))


def outside_summary(document):
    import re

    return re.sub(r"(<pre>).*?(</pre>)", r"\1\2", document, count=1, flags=re.S)


@pytest.mark.parametrize(
    "blocked", [("report.html",), ("report.json",), ("report.html", "report.json")]
)
def test_report_outputs_are_updated_independently(tmp_path, monkeypatch, blocked):
    from pathlib import Path

    from nanolab.one_shot.report import finalize_operational_outcome

    render_report(manifest(), evidence(), tmp_path / "report.html")
    original = json.loads((tmp_path / "report.json").read_text())
    document = (tmp_path / "report.html").read_text()
    outcome = {"state": "failed", "cause": "primary failed", "exitCode": 143}
    write = Path.write_text
    faults = {name: OSError(name + " write unavailable") for name in blocked}
    attempts = []

    def reject_write(path, data, *a, **kw):
        attempts.append(path.name)
        if path.name in faults:
            raise faults[path.name]
        return write(path, data, *a, **kw)

    monkeypatch.setattr(Path, "write_text", reject_write)
    with pytest.raises(OSError, match=blocked[0]) as caught:
        finalize_operational_outcome(tmp_path, outcome)
    assert caught.value is faults[blocked[0]]
    assert attempts == ["report.html", "report.json"]
    if len(blocked) == 2:
        assert "report.json write unavailable" in str(caught.value.__notes__)
    for name in ("report.html", "report.json"):
        if name in blocked:
            continue
        updated = (
            embedded_summary((tmp_path / name).read_text())
            if name.endswith("html")
            else json.loads((tmp_path / name).read_text())
        )
        assert updated.pop("operationalOutcome") == outcome
        assert updated == {
            k: v for k, v in original.items() if k != "operationalOutcome"
        }
    assert outside_summary((tmp_path / "report.html").read_text()) == outside_summary(
        document
    )


@pytest.mark.parametrize("remaining", [None, "report.json", "report.html"])
def test_finalization_updates_each_existing_report(tmp_path, remaining):
    from nanolab.one_shot.report import finalize_operational_outcome

    if remaining is not None:
        render_report(manifest(), None, tmp_path / "report.html")
        (
            tmp_path / ("report.html" if remaining == "report.json" else "report.json")
        ).unlink()
    outcome = {"state": "failed", "cause": "early failed", "exitCode": 1}
    finalize_operational_outcome(tmp_path, outcome)
    if remaining is None:
        assert not list(tmp_path.iterdir())
    else:
        updated = (
            embedded_summary((tmp_path / remaining).read_text())
            if remaining.endswith("html")
            else json.loads((tmp_path / remaining).read_text())
        )
        assert updated["operationalOutcome"] == outcome


@pytest.mark.parametrize(
    "change", ["operationalOutcome", "mode", "missing", "ambiguous", "malformed"]
)
def test_report_finalization_accepts_only_outcome_divergence(tmp_path, change):
    import html

    from nanolab.one_shot.report import finalize_operational_outcome

    render_report(manifest(), evidence(), tmp_path / "report.html")
    document = (tmp_path / "report.html").read_text()
    summary = json.loads((tmp_path / "report.json").read_text())
    previous = json.dumps(summary, indent=2, allow_nan=False)
    old = "<pre>" + html.escape(previous) + "</pre>"
    assert old in document
    changed = dict(summary)
    changed[change] = (
        {"state": "completed", "exitCode": 0}
        if change == "operationalOutcome"
        else "tampered"
    )
    replacement = "<pre>" + html.escape(json.dumps(changed, indent=2)) + "</pre>"
    if change == "missing":
        replacement = ""
    elif change == "ambiguous":
        replacement = old + old
    elif change == "malformed":
        replacement = "<pre>{invalid JSON}</pre>"
    document = (
        document.replace(old, replacement) + "\r\n<!-- chart bytes preserved -->\r\n"
    )
    (tmp_path / "report.html").write_text(document)
    before = (tmp_path / "report.html").read_bytes()
    outcome = {"state": "failed", "cause": "operational failure", "exitCode": 1}
    if change == "operationalOutcome":
        finalize_operational_outcome(tmp_path, outcome)
        actual = (tmp_path / "report.html").read_bytes().decode()
        assert embedded_summary(actual) == json.loads(
            (tmp_path / "report.json").read_text()
        )
        assert outside_summary(actual) == outside_summary(document)
        assert embedded_summary(actual)["operationalOutcome"] == outcome
    else:
        with pytest.raises(ValueError, match=r"summary|Expecting property name"):
            finalize_operational_outcome(tmp_path, outcome)
        assert (tmp_path / "report.html").read_bytes() == before
        if change == "mode":
            assert json.loads((tmp_path / "report.json").read_text()) == summary


@pytest.mark.parametrize("blocked", ["report.json", "report.html"])
def test_unreadable_report_does_not_block_valid_other_evidence(
    tmp_path, monkeypatch, blocked
):
    from pathlib import Path

    from nanolab.one_shot.report import finalize_operational_outcome

    render_report(manifest(), None, tmp_path / "report.html")
    read_text, read_bytes = Path.read_text, Path.read_bytes

    def text(path, *a, **kw):
        if path.name == blocked:
            raise OSError(blocked + " read unavailable")
        return read_text(path, *a, **kw)

    def binary(path):
        if path.name == blocked:
            raise OSError(blocked + " read unavailable")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_text", text)
    monkeypatch.setattr(Path, "read_bytes", binary)
    outcome = {"state": "failed", "cause": "failed", "exitCode": 1}
    with pytest.raises(OSError, match=blocked):
        finalize_operational_outcome(tmp_path, outcome)
    other = "report.html" if blocked == "report.json" else "report.json"
    updated = (
        embedded_summary((tmp_path / other).read_text())
        if other.endswith("html")
        else json.loads((tmp_path / other).read_text())
    )
    assert updated["operationalOutcome"] == outcome
