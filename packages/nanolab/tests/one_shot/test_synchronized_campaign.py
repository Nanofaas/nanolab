"""Nine-round accounting does not inherit the legacy steady-phase filter."""

from copy import deepcopy
from typing import Any, cast

import pytest

from nanolab.one_shot.campaign import edge_outcomes, paired_summary
from nanolab.one_shot.models import CampaignManifest
from nanolab.tasks.one_shot.conservation import RunEvidence
from nanolab.tasks.one_shot.trace import TraceArtifact, concurrent_trace
from tests.one_shot.test_campaign_statistics import block
from tests.one_shot.test_multi_function_trace import inputs


def synchronized_block(index=0):
    settings, records = inputs()
    trace = concurrent_trace(
        records,
        settings,
        period=360,
        seed=7,
        aliases={name: name for name in settings.functions},
    )
    result = block(index)
    for mode, run in result["runs"].items():
        manifest = CampaignManifest.model_validate(run["manifest"])
        manifest = manifest.model_copy(
            update={
                "trace_sha256": trace.sha256,
                "functions": trace.functions,
                "period_seconds": 360,
            }
        )
        cells = []
        for epoch, window in enumerate(trace.windows):
            for node, rates in window.items():
                for name, rate in rates.items():
                    count = round(rate * 360)
                    cloud = count // (
                        3 if mode == "baseline" else 4 if mode == "ewma" else 5
                    )
                    cells.append(
                        {
                            "epoch": epoch,
                            "origin": node,
                            "function": name,
                            "planned": count,
                            "completed": count,
                            "errors": 0,
                            "destinations": {"cloud": count}
                            if node == "cloud"
                            else {node: count - cloud, "cloud": cloud},
                        }
                    )
        total = sum(cell["planned"] for cell in cells)
        evidence = RunEvidence(
            planned=total,
            emitted=total,
            received=total,
            attempts=total,
            completed=total,
            errors=0,
            generator_deficit=0,
            valid=True,
            violations=[],
            cells=cast(Any, cells),
        )
        run.update(
            manifest=manifest.model_dump(),
            evidence=evidence.model_dump(),
            trace=trace.model_dump(),
        )
    return result


def test_all_nine_rounds_and_five_functions_enter_edge_denominator():
    run = synchronized_block()["runs"]["ewma"]
    trace = TraceArtifact.model_validate(run["trace"])
    evidence = RunEvidence.model_validate(run["evidence"])
    result = edge_outcomes(evidence, trace)
    edge = [cell for cell in evidence.cells if cell["origin"] != "cloud"]
    assert result["planned"] == sum(cast(int, cell["planned"]) for cell in edge)
    assert result["cloud"] == sum(
        cast(dict, cell["destinations"])["cloud"] for cell in edge
    )
    assert result["completed"] == result["planned"] and len(edge) == 225


@pytest.mark.parametrize("fault", ["missing", "duplicate", "function"])
def test_synchronized_accounting_rejects_wrong_physical_cell(fault):
    run = synchronized_block()["runs"]["ewma"]
    evidence = deepcopy(run["evidence"])
    if fault == "missing":
        evidence["cells"].pop()
    elif fault == "duplicate":
        evidence["cells"].append(evidence["cells"][0])
    else:
        evidence["cells"][0]["function"] = "wrong"
    with pytest.raises(ValueError, match="missing or duplicated"):
        edge_outcomes(
            RunEvidence.model_validate(evidence),
            TraceArtifact.model_validate(run["trace"]),
        )


def test_paired_bootstrap_declares_all_nine_synchronized_rounds():
    result = paired_summary(
        [synchronized_block(i) for i in range(12)], seed=7, draws=100
    )
    assert result["complete"] and result["completionGuard"]["passed"]
    assert (
        result["primary"]["denominator"]
        == "planned-edge-originals-all-nine-synchronized-epochs"
    )
    assert result["analysisEpochIndices"] == list(range(9))


def protocol_fixture(tmp_path, monkeypatch, *, rust_aliases=False):
    from types import SimpleNamespace

    from nanolab.config.one_shot import ArtifactReference
    from nanolab.one_shot.profiles import CalibrationProfiles
    from nanolab.tasks.one_shot.trace import cloud_capacity_requirements
    from tests.one_shot.test_campaign_statistics import protocol_fixture as legacy

    settings, records = inputs()
    _old, _profiles, qualification, _trace = legacy(tmp_path)
    aliases = {name: name for name in settings.functions}
    if rust_aliases:
        data = settings.model_dump(mode="json", by_alias=True, exclude_none=True)
        data["functions"] = {
            key + "-rust": value for key, value in data["functions"].items()
        }
        for node in data["nodes"]:
            node["functionLimits"] = {
                key + "-rust": value for key, value in node["functionLimits"].items()
            }
        settings = type(settings).model_validate(data)
        aliases = {key: key.removesuffix("-rust") for key in settings.functions}
    trace = concurrent_trace(
        records, settings, period=360, seed=settings.seed, aliases=aliases
    )
    records += [
        {
            "node": "cloud",
            "replicaVector": dict.fromkeys(trace.functions, 7),
            "rates": rates,
            "trials": [],
        }
        for rates in cloud_capacity_requirements(trace, "cloud")
    ]
    profiles = CalibrationProfiles(
        {
            node.id: cast(
                Any,
                SimpleNamespace(
                    functions=[
                        {
                            "function": name,
                            "serviceSeconds": 0.01,
                            "memoryMiB": 64,
                            "validity": {"maxReplicas": 35},
                        }
                        for name in trace.functions
                    ]
                ),
            )
            for node in settings.nodes
        },
        "a" * 64,
        "sha256:" + "a" * 64,
        concurrent_capacities=records,
    )
    qualification = qualification.model_copy(
        update={
            "period_seconds": 360,
            "profile_sha256": profiles.sha256,
            "environment_fingerprint": profiles.environment_fingerprint,
            "period_candidates": [360],
            "max_trace_resolution": 360,
        }
    )
    assert trace.phase_ids is not None
    qualification = qualification.model_copy(
        update={
            "matrix": [
                {
                    "cell": {"id": phase, "rates": window},
                    "result": qualification.model_dump(),
                    "load": {
                        "method": "owned-http-sdk-contention-v1",
                        "valid": True,
                        "physicalCompletions": 100,
                        "emitted": 100,
                        "generatorDeficit": 0,
                    },
                }
                for phase, window in zip(trace.phase_ids, trace.windows, strict=True)
            ]
        }
    )
    settings = settings.model_copy(
        update={
            "profile": ArtifactReference(
                path=tmp_path / "profile.json", sha256=profiles.sha256
            ),
            "qualification": _old.qualification,
            "runtime_distribution": _old.runtime_distribution,
        }
    )
    monkeypatch.setattr(
        "nanolab.one_shot.profiles.read_profiles", lambda _settings: profiles
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.read_qualification",
        lambda _settings: qualification,
    )
    from nanolab.one_shot.campaign import freeze_campaign_protocol

    protocol = freeze_campaign_protocol(
        profiles=profiles, qualification=qualification, pilot_rows=[], settings=settings
    )
    return protocol, settings, trace


@pytest.mark.parametrize("rust_aliases", [False, True])
def test_freezer_and_public_scenarios_keep_five_aliases_and_nine_rounds(
    tmp_path, monkeypatch, rust_aliases
):
    import yaml

    from nanolab.config import ScenarioConfig
    from nanolab.one_shot.campaign import (
        read_campaign_binding,
        write_campaign_scenarios,
    )

    protocol, settings, trace = protocol_fixture(
        tmp_path, monkeypatch, rust_aliases=rust_aliases
    )
    assert protocol["schemaVersion"] == 2
    assert protocol["analysis"]["analysisEpochIndices"] == list(range(9))
    assert protocol["analysis"]["functionAliases"] == {
        name: name.removesuffix("-rust") for name in settings.functions
    }
    assert (
        protocol["analysis"]["denominator"]
        == "planned-edge-originals-all-nine-synchronized-epochs"
    )
    paths = write_campaign_scenarios(protocol, tmp_path / "campaign")
    assert len(paths) == 6
    for path in paths:
        scenario = ScenarioConfig.model_validate(yaml.safe_load(path.read_text()))
        current = scenario.one_shot
        assert current is not None and current.experiment is not None
        assert len(current.experiment.windows) == 9
        assert all(window.epochs == 1 for window in current.experiment.windows)
        assert current.experiment.warmup_seconds == 720
        binding = read_campaign_binding(current)
        assert binding is not None and binding["trace"] == trace
        assert len(binding["trace"].windows) * binding["trace"].period_seconds == 3240


@pytest.mark.parametrize(
    "fault", ["epochs", "aliases", "permuted-aliases", "denominator", "version"]
)
def test_protocol_rejects_changed_analysis_before_acquisition(
    tmp_path, monkeypatch, fault
):
    from nanolab.one_shot.models import CampaignProtocol

    protocol, _settings, _trace = protocol_fixture(tmp_path, monkeypatch)
    if fault == "epochs":
        protocol["analysis"]["analysisEpochIndices"] = [2, 3, 4, 5, 6, 7, 8]
    elif fault == "aliases":
        protocol["analysis"]["functionAliases"]["f1"] = "f0"
    elif fault == "permuted-aliases":
        protocol["analysis"]["functionAliases"].update(f0="f1", f1="f0")
    elif fault == "denominator":
        protocol["analysis"]["denominator"] = "last-four"
    else:
        protocol["schemaVersion"] = 1
    with pytest.raises(ValueError, match="synchronized"):
        CampaignProtocol.model_validate(protocol)


def test_report_retains_every_node_function_epoch_and_physical_routes():
    from nanolab.one_shot.report import summarize

    run = synchronized_block()["runs"]["ewma"]
    evidence = RunEvidence.model_validate(run["evidence"])
    summary = summarize(CampaignManifest.model_validate(run["manifest"]), evidence)
    assert summary["outcomesByCell"] == evidence.model_dump()["cells"]
    assert len(summary["edgeOutcomesByFunction"]) == 5
    assert len(summary["edgeOutcomesByEpoch"]) == 9
    assert len(summary["edgeOutcomesByNode"]) == 5
    assert (
        sum(row["planned"] for row in summary["edgeOutcomesByFunction"].values())
        == summary["edgeOriginals"]["planned"]
    )
    assert (
        summary["cloudDirect"]["planned"] + summary["edgeOriginals"]["planned"]
        == evidence.planned
    )
    assert (
        summary["analysisScope"]
        == "all-nine-synchronized-epochs; no steady-state claim"
    )
    bad = evidence.model_copy(update={"valid": False})
    assert (
        summarize(CampaignManifest.model_validate(run["manifest"]), bad)["conforming"]
        is False
    )


def test_report_uses_complete_balanced_capacity_record_when_scalar_is_absent():
    from nanolab.one_shot.report import summarize

    run = synchronized_block()["runs"]["ewma"]
    settings, records = inputs()
    from nanolab.one_shot.capacity import comparison_replica_vectors

    vectors = comparison_replica_vectors(
        settings, {name: name for name in settings.functions}
    )
    manifest = CampaignManifest.model_validate(run["manifest"])
    parameters = cast(dict[str, Any], dict(manifest.parameters))
    parameters.update(
        capacities=None, concurrentCapacities=records, staticReplicaVectors=vectors
    )
    value = RunEvidence.model_validate(run["evidence"])
    observations = dict(value.observations)
    observations["plannedWindows"] = run["trace"]["windows"]
    summary = summarize(
        manifest.model_copy(update={"parameters": parameters}),
        value.model_copy(update={"observations": observations}),
    )
    assert len(summary["phaseNodes"]) == 270
    for cell in summary["phaseNodes"]:
        assert cell["capacity"] > 0 and cell["loadToCapacity"] > 0
        assert cell["capacityScope"] == "measured-balanced-reference-vector"
        assert cell["replicaVector"] == vectors[cell["node"]]
