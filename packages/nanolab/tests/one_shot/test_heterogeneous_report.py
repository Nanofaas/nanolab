"""Keep experiment coverage separate from conforming and favorable outcomes."""

import json
from importlib.resources import files

import pytest
import yaml

from nanolab.config import ScenarioConfig
from nanolab.one_shot.models import CampaignManifest
from nanolab.one_shot.report import summarize
from nanolab.tasks.one_shot.conservation import RunEvidence
from tests.one_shot.test_campaign_statistics import block


def observed(mode="baseline", *, peer=False):
    run = block()["runs"][mode]
    manifest = CampaignManifest.model_validate(run["manifest"])
    manifest.parameters.update(
        {
            "nodes": [
                {"id": node, "kind": "cloud" if node == "cloud" else "edge"}
                for node in manifest.nodes
            ],
            "phaseIds": run["trace"]["phase_ids"],
            "requestedWindows": run["trace"]["windows"],
            "capacities": dict.fromkeys(manifest.nodes, 10),
            "peerGraph": {"edge-1": ["edge-2"], "edge-2": ["edge-1"]},
            "nodeProfiles": {
                node: {"functions": [{"function": "work", "serviceSeconds": 0.1}]}
                for node in manifest.nodes
            },
            "nodeFunctions": {
                node: {"work": {"cpu": 0.5, "memoryMiB": 256, "maxReplicas": 2}}
                for node in manifest.nodes
            },
        }
    )
    evidence = RunEvidence.model_validate(run["evidence"])
    evidence.observations["plannedWindows"] = run["trace"]["windows"]
    evidence.observations["metricSamples"] = [
        {
            "at": 1.0,
            "node": "edge-1",
            "function": "work",
            "replica": 0,
            "metrics": {"nanofaas_runtime_active_handlers": 1},
        }
        for _ in range(3)
    ]
    if peer:
        evidence.cells[0]["destinations"] = {"edge-2": 70, "cloud": 30}
    return manifest, evidence


def test_baseline_without_peer_is_conforming_but_does_not_claim_native_coverage():
    manifest, evidence = observed()
    result = summarize(manifest, evidence)
    assert result["conforming"]
    assert not result["coverageSatisfied"]
    assert result["cloudDirect"]["completed"] == 5400
    assert result["originDestination"]["edge-1"]["cloud"] == 1620
    cell = result["phaseNodes"][0]
    assert cell["capacity"] == 10 and cell["loadToCapacity"] == 1
    assert cell["demandSeconds"] == 0.1 and cell["cpuQuota"] == 0.5


def test_native_coverage_and_failed_accounting_are_independent():
    manifest, evidence = observed("ewma", peer=True)
    result = summarize(manifest, evidence)
    assert result["coverageSatisfied"] and result["conforming"]
    evidence.valid = False
    evidence.violations.append("missing-sdk-proof")
    result = summarize(manifest, evidence)
    assert result["coverageSatisfied"] and not result["conforming"]


def test_paired_html_preserves_null_main_and_partial_intervals(tmp_path):
    from nanolab.one_shot.report import render_paired_campaign

    rows = [block(i) for i in range(12)]
    for row in rows:
        for run in row["runs"].values():
            for cell in run["evidence"]["cells"]:
                cell["destinations"] = {cell["origin"]: cell["completed"]}
    path = render_paired_campaign(rows, tmp_path / "main.html", seed=7)
    result = json.loads(path.with_suffix(".json").read_text())
    assert result["complete"] and result["primary"]["estimate"] == 0
    assert result["primary"]["confidence95"] == [0, 0]
    assert "paired-whole-block" in path.read_text()
    path = render_paired_campaign(rows[:-1], tmp_path / "partial.html", seed=7)
    result = json.loads(path.with_suffix(".json").read_text())
    assert not result["complete"] and result["primary"]["confidence95"] is None


@pytest.mark.parametrize("stage", ["calibration", "qualification", "pilot"])
def test_heterogeneous_presets_ship_the_same_frozen_topology(stage):
    path = files("nanolab").joinpath(
        f"assets/presets/scenarios/one-shot-heterogeneous-{stage}.yaml"
    )
    config = ScenarioConfig.model_validate(yaml.safe_load(path.read_text()))
    assert config.one_shot is not None
    settings = config.one_shot
    assert settings.purpose == "scientific-experiment"
    assert [n.cpus for n in settings.nodes] == [1, 1, 2, 2, 3, 8]
    assert [
        n.function_limits["one-shot-workload-rust"].cpu for n in settings.nodes
    ] == [0.35, 0.5, 0.6, 0.7, 0.85, 1]
    assert settings.neighbors is not None
    assert settings.neighbors["edge-2"] == ["edge-1", "edge-3", "edge-5"]
    assert settings.flow_quantum == 0.2
    assert settings.observability.logs.enabled


def test_standalone_run_html_exposes_graph_matrix_and_measured_capacity(tmp_path):
    from nanolab.one_shot.report import render_report

    manifest, evidence = observed("ewma", peer=True)
    path = render_report(manifest, evidence, tmp_path / "run.html")
    content = path.read_text().replace("\\u002f", "/")
    assert "Horizontal peer graph" in content
    assert "origin/destination" in content
    assert "effective load / measured capacity" in content
    assert json.loads(path.with_suffix(".json").read_text())["coverageSatisfied"]
