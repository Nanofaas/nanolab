import json
from typing import Literal

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
    path = render_campaign(tmp_path)
    data = json.loads(path.with_suffix(".json").read_text())
    assert data["validComparison"]
    assert all(
        row["confidence95"] is None for row in data["betweenRepetitions"].values()
    )
    (tmp_path / "rep-0-ewma/evidence.json").unlink()
    render_campaign(tmp_path)
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
    path = render_campaign(tmp_path)
    assert json.loads(path.with_suffix(".json").read_text())["validComparison"]
    (tmp_path / "rep-0-ewma/manifest.json").write_text(
        manifest("baseline").model_dump_json()
    )
    render_campaign(tmp_path)
    assert not json.loads(path.with_suffix(".json").read_text())["validComparison"]


def test_changed_runtime_package_invalidates_comparison(tmp_path):
    for mode in MODES:
        directory = tmp_path / ("rep-0-" + mode)
        directory.mkdir()
        value = manifest(mode)
        value.parameters["nanolabPackageSha256"] = mode
        (directory / "manifest.json").write_text(value.model_dump_json())
        (directory / "evidence.json").write_text(evidence().model_dump_json())
    path = render_campaign(tmp_path)
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
    path = render_campaign(tmp_path)
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
    path = render_campaign(tmp_path)
    assert not json.loads(path.with_suffix(".json").read_text())["validComparison"]
