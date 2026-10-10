"""Statistical replication uses paired whole traces, never individual requests."""

import copy
from importlib import import_module
from typing import Literal, cast

import pytest

from nanolab.config.one_shot import ExperimentSettings
from nanolab.one_shot.models import CampaignManifest
from nanolab.tasks.one_shot.conservation import RunEvidence
from nanolab.tasks.one_shot.trace import TraceArtifact


def campaign():
    return import_module("nanolab.one_shot.campaign")


def block(index=0, *, role="main", n=12, cloud_direct=10):
    nodes = [f"edge-{i}" for i in range(1, 6)] + ["cloud"]
    trace = TraceArtifact(
        nodes=nodes,
        functions=["work"],
        period_seconds=10,
        flow_quantum=1,
        seed=7,
        payloads={"work": {}},
        windows=[{node: {"work": 10.0} for node in nodes} for _ in range(54)],
        node_roles={
            node: "cloud-direct" if node == "cloud" else "edge" for node in nodes
        },
        phase_ids=[f"phase-{i // 6}" for i in range(54)],
    )
    runs = {}
    for mode, cloud in [("baseline", 30), ("oracle", 10), ("ewma", 20)]:
        manifest = CampaignManifest(
            provider="multipass",
            purpose="scientific-experiment",
            environment_fingerprint="fp",
            source_commit="a" * 40,
            profile_sha256="b" * 64,
            qualification_sha256="c" * 64,
            trace_sha256=trace.sha256,
            seed=index + 7,
            nodes=nodes,
            functions=["work"],
            period_seconds=10,
            lead_seconds=1,
            flow_quantum=1,
            modes=["baseline", "oracle", "ewma"],
            mode=cast(Literal["baseline", "oracle", "ewma"], mode),
            run_id=f"{index}-{mode}",
            parameters={
                "campaign": {
                    "block": index,
                    "seed": index + 7,
                    "role": role,
                    "plannedBlocks": n,
                    "protocolSha256": "d" * 64,
                }
            },
        )
        cells = [
            {
                "epoch": epoch,
                "origin": node,
                "function": "work",
                "planned": 100,
                "completed": 100,
                "errors": 0,
                "censored": 0,
                "generator_deficit": 0,
                "destinations": (
                    {"cloud": cloud_direct, node: 100 - cloud_direct}
                    if node == "cloud"
                    else {node: 100 - cloud, "cloud": cloud}
                ),
            }
            for epoch in range(54)
            for node in nodes
        ]
        # Cloud-direct completions do not enter the edge contrast.
        for cell in cells:
            if cell["origin"] == "cloud":
                cell["destinations"] = {"cloud": 100}
        evidence = RunEvidence(
            planned=32400,
            emitted=32400,
            received=32400,
            attempts=32400,
            completed=32400,
            errors=0,
            censored=0,
            generator_deficit=0,
            valid=True,
            violations=[],
            cells=cells,
            observations={"loadExecuted": True},
        )
        runs[mode] = {
            "manifest": manifest.model_dump(),
            "evidence": evidence.model_dump(),
            "trace": trace.model_dump(),
        }
    return {
        "block": index,
        "role": role,
        "plannedBlocks": n,
        "protocolSha256": "d" * 64,
        "runs": runs,
    }


def test_requests_are_not_independent_repetitions():
    result = campaign().paired_summary([block(i) for i in range(12)], seed=7)
    assert result["primary"]["estimate"] == pytest.approx(-0.10)
    assert result["primary"]["blockCount"] == 12
    assert result["primary"]["confidence95"] == pytest.approx([-0.10, -0.10])
    assert (
        result["primary"]["denominator"]
        == "planned-edge-originals-last-four-epochs-per-phase"
    )
    assert result["complete"]
    assert result["completionGuard"]["passed"]
    assert result["secondaryOracle"]["estimate"] == pytest.approx(-0.20)


def test_pairing_requires_all_modes_and_matching_scope():
    rows = [block(i) for i in range(12)]
    rows[0]["runs"]["ewma"]["manifest"]["profile_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="scope"):
        campaign().paired_summary(rows, seed=7)
    rows = [block(i) for i in range(12)]
    rows[0]["runs"].pop("ewma")
    result = campaign().paired_summary(rows, seed=7)
    assert not result["complete"]
    assert result["primary"]["confidence95"] is None
    assert result["excludedBlocks"]


def test_cloud_direct_is_excluded_from_primary_metric():
    rows = [block(i) for i in range(12)]
    before = campaign().paired_summary(rows, seed=7)
    for row in rows:
        evidence = row["runs"]["ewma"]["evidence"]
        for cell in evidence["cells"]:
            if cell["origin"] == "cloud":
                cell["completed"] = 0
                cell["errors"] = 100
                cell["destinations"] = {}
                evidence["completed"] -= 100
                evidence["errors"] += 100
    after = campaign().paired_summary(rows, seed=7)
    assert after["primary"] == before["primary"]
    assert after["completionGuard"] == before["completionGuard"]


def test_pilot_is_excluded_from_main_analysis():
    rows = [block(i, role="pilot", n=6) for i in range(6)] + [
        block(i) for i in range(12)
    ]
    result = campaign().paired_summary(rows, seed=7)
    assert result["primary"]["blockCount"] == 12
    assert result["pilotBlockCount"] == 6


def test_sample_size_is_frozen_before_main():
    assert campaign().plan_repetitions([0.05] * 6) == 12
    values = [-0.08, 0.08] * 3
    assert campaign().plan_repetitions(values) == 30
    with pytest.raises(ValueError, match=r"36|budget"):
        campaign().plan_repetitions([-0.5, 0.5] * 3)
    with pytest.raises(ValueError, match="pilot"):
        campaign().plan_repetitions([0.1] * 5)


def test_interrupted_campaign_is_incomplete():
    result = campaign().paired_summary([block(i) for i in range(11)], seed=7)
    assert not result["complete"]
    assert result["primary"]["confidence95"] is None
    assert not result["confirmatory"]
    assert result["missingBlocks"] == [11]


def test_completion_loss_is_guarded_and_bad_runs_are_retained():
    rows = [block(i) for i in range(12)]
    for row in rows:
        evidence = row["runs"]["ewma"]["evidence"]
        for cell in evidence["cells"]:
            if cell["origin"] != "cloud":
                cell["completed"] -= 3
                cell["errors"] += 3
                cell["destinations"][cell["origin"]] -= 3
                evidence["completed"] -= 3
                evidence["errors"] += 3
    result = campaign().paired_summary(rows, seed=7)
    assert not result["completionGuard"]["passed"]
    rows[0]["runs"]["oracle"]["evidence"]["valid"] = False
    rows[0]["invalidity"] = "algorithm-associated"
    result = campaign().paired_summary(rows, seed=7)
    assert not result["complete"]
    assert result["primary"]["confidence95"] is None
    assert result["excludedBlocks"][0]["block"] == 0
    assert not result["confirmatory"]


@pytest.mark.parametrize("bad", ["duplicate", "denominator", "trace"])
def test_registry_and_counts_cannot_hide_inconsistent_data(bad):
    rows = [block(i) for i in range(12)]
    if bad == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif bad == "denominator":
        rows[0]["runs"]["ewma"]["evidence"]["cells"][0]["planned"] += 1
    else:
        rows[0]["runs"]["ewma"]["trace"]["seed"] += 1
    with pytest.raises(ValueError, match=r"duplicate|planned|trace"):
        campaign().paired_summary(rows, seed=7)


def test_protocol_reference_and_block_are_required_together():
    rates = {"edge": {"work": 1}}
    with pytest.raises(ValueError, match="together"):
        ExperimentSettings.model_validate(
            {"windows": [{"id": "x", "rates": rates}], "campaignBlock": 0}
        )
    with pytest.raises(ValueError, match="together"):
        ExperimentSettings.model_validate(
            {
                "windows": [{"id": "x", "rates": rates}],
                "campaignProtocol": {"path": "protocol.json", "sha256": "a" * 64},
            }
        )


def protocol_fixture(tmp_path):
    import json

    from nanolab.config.one_shot import OneShotConfig, ProtocolSettings
    from nanolab.one_shot.models import TimingQualification
    from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash
    from nanolab.tasks.one_shot.trace import capacity_trace
    from tests.one_shot.test_node_profiles import fixture, read

    data, index = fixture(tmp_path)
    for n in range(3, 6):
        node = f"edge-{n}"
        data["nodes"].insert(-1, {"id": node, "kind": "edge"})
        doc = json.loads((tmp_path / "edge-2.json").read_bytes())
        doc.update(profileId=node, environmentFingerprint="sha256:" + str(n + 2) * 64)
        path = tmp_path / (node + ".json")
        path.write_bytes(canonical_bytes(doc))
        index["nodes"][node] = {
            "path": path.name,
            "sha256": content_hash(path.read_bytes()),
        }
        index["nodeFingerprints"][node] = doc["environmentFingerprint"]
    cloud = data["nodes"][-1]
    cloud.update(
        cpus=8,
        memoryCapacityMiB=896,
        functionLimits={"f": {"cpu": 1, "memoryMiB": 128, "maxReplicas": 7}},
    )
    doc = json.loads((tmp_path / "cloud.json").read_bytes())
    doc["functions"][0]["validity"]["maxReplicas"] = 7
    (tmp_path / "cloud.json").write_bytes(canonical_bytes(doc))
    index["nodes"]["cloud"]["sha256"] = content_hash(
        (tmp_path / "cloud.json").read_bytes()
    )
    capacity = json.loads((tmp_path / "capacity.json").read_bytes())
    capacity["capacities"] = {
        **{f"edge-{n}": 10.0 for n in range(1, 6)},
        "cloud": 100.0,
    }
    (tmp_path / "capacity.json").write_bytes(canonical_bytes(capacity))
    index["capacity"]["sha256"] = content_hash(
        (tmp_path / "capacity.json").read_bytes()
    )
    profiles = read(tmp_path, data, index)
    settings = OneShotConfig.model_validate(data)
    trace = capacity_trace(profiles.capacities, settings, 20, 7)
    result = TimingQualification(
        provider="multipass",
        purpose="scientific-experiment",
        environment_fingerprint=profiles.environment_fingerprint,
        profile_sha256=profiles.sha256,
        qualified=True,
        period_seconds=20,
        lead_seconds=1.2,
        quantile=0.95,
        quantile_seconds=0.1,
        margin_seconds=0.2,
        epsilon=0.05,
        sample_count=50,
        censored_count=0,
        samples_seconds=[0.1] * 50,
        minimum_samples=50,
        period_candidates=[5, 10, 20, 30, 60],
        max_trace_resolution=60,
        ready_samples_seconds=[0.2] * 50,
        protocol=ProtocolSettings().model_dump(by_alias=True),
    )
    assert trace.phase_ids is not None
    matrix = [
        {
            "cell": {"id": trace.phase_ids[i], "rates": trace.windows[i]},
            "result": result.model_dump(),
            "load": {
                "method": "owned-http-sdk-contention-v1",
                "valid": True,
                "physicalCompletions": 100,
                "emitted": 100,
                "generatorDeficit": 0,
            },
        }
        for i in range(0, 54, 6)
    ]
    result = result.model_copy(update={"matrix": matrix})
    path = tmp_path / "timing.json"
    path.write_bytes(canonical_bytes(result.model_dump()))
    data["qualification"] = {"path": path, "sha256": content_hash(path.read_bytes())}
    distribution = tmp_path / "distribution.json"
    distribution.write_text("{}\n")
    data["runtimeDistribution"] = {
        "path": distribution,
        "sha256": content_hash(distribution.read_bytes()),
    }
    settings = OneShotConfig.model_validate(data)
    return settings, profiles, result, trace


def pilot_rows(settings, profiles, qualification, trace):
    from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash

    registered = campaign().freeze_campaign_protocol(
        profiles=profiles, qualification=qualification, pilot_rows=[], settings=settings
    )
    registered_sha = content_hash(canonical_bytes(registered))
    rows = [block(i, role="pilot", n=6) for i in range(6)]
    for row in rows:
        row["protocolSha256"] = registered_sha
        for mode, run in row["runs"].items():
            manifest = run["manifest"]
            descriptor = registered["blocks"][row["block"]]
            manifest["modes"] = descriptor["modes"].copy()
            manifest["seed"] = descriptor["seed"]
            manifest["parameters"]["campaign"]["protocolSha256"] = registered_sha
            manifest.update(
                profile_sha256=settings.profile.sha256,
                qualification_sha256=settings.qualification.sha256,
                environment_fingerprint=profiles.environment_fingerprint,
                source_commit=profiles.by_node["cloud"].source_commit,
                trace_sha256=trace.sha256,
                functions=trace.functions,
                period_seconds=trace.period_seconds,
            )
            run["trace"] = trace.model_dump()
            cloud_fraction = {"baseline": 0.3, "oracle": 0.1, "ewma": 0.2}[mode]
            cells = []
            for epoch, window in enumerate(trace.windows):
                for node, rates in window.items():
                    for name, rate in rates.items():
                        planned = round(rate * trace.period_seconds)
                        cloud = round(planned * cloud_fraction)
                        cells.append(
                            {
                                "epoch": epoch,
                                "origin": node,
                                "function": name,
                                "planned": planned,
                                "completed": planned,
                                "errors": 0,
                                "destinations": {"cloud": planned}
                                if node == "cloud"
                                else {node: planned - cloud, "cloud": cloud},
                            }
                        )
            total = sum(cell["planned"] for cell in cells)
            run["evidence"].update(
                cells=cells,
                planned=total,
                emitted=total,
                received=total,
                attempts=total,
                completed=total,
            )
    return rows


def test_frozen_pilot_and_main_protocols_balance_all_six_orders(tmp_path):
    settings, profiles, qualification, trace = protocol_fixture(tmp_path)
    pilot = campaign().freeze_campaign_protocol(
        profiles=profiles, qualification=qualification, pilot_rows=[], settings=settings
    )
    assert pilot["role"] == "pilot" and pilot["plannedBlocks"] == 6
    assert len({tuple(b["modes"]) for b in pilot["blocks"]}) == 6
    rows = pilot_rows(settings, profiles, qualification, trace)
    main = campaign().freeze_campaign_protocol(
        profiles=profiles,
        qualification=qualification,
        pilot_rows=rows,
        settings=settings,
    )
    assert main["role"] == "main" and main["plannedBlocks"] == 12
    assert len(main["blocks"]) == 12
    assert len(main["replacements"]) == 2
    assert main["trace"] == trace.model_dump()
    assert main["analysis"]["bootstrapDraws"] == 10000
    assert main["analysis"]["completionGuard"] == -0.02
    assert main == campaign().freeze_campaign_protocol(
        profiles=profiles,
        qualification=qualification,
        pilot_rows=rows,
        settings=settings,
    )


def test_protocol_generation_is_hash_bound_and_uses_existing_public_workflow(tmp_path):
    import json

    import yaml

    from nanolab.config import ScenarioConfig
    from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash

    settings, profiles, qualification, _trace = protocol_fixture(tmp_path)
    protocol = campaign().freeze_campaign_protocol(
        profiles=profiles, qualification=qualification, pilot_rows=[], settings=settings
    )
    output = tmp_path / "pilot"
    files = campaign().write_campaign_scenarios(protocol, output)
    assert len(files) == 6
    reference = output / "protocol.json"
    assert json.loads(reference.read_bytes()) == protocol
    for number, path in enumerate(files):
        config = ScenarioConfig.model_validate(yaml.safe_load(path.read_text()))
        assert config.one_shot is not None and config.one_shot.experiment is not None
        assert config.one_shot.experiment.campaign_protocol is not None
        assert config.workflow == "one-shot-experiment"
        assert config.one_shot.experiment.repetitions == 1
        assert config.one_shot.experiment.campaign_block == number
        assert config.one_shot.experiment.campaign_protocol.sha256 == content_hash(
            canonical_bytes(protocol)
        )
        assert len(config.one_shot.experiment.windows) == 9
    assert campaign().write_campaign_scenarios(protocol, output) == files
    changed = copy.deepcopy(protocol)
    changed["seed"] += 1
    with pytest.raises(ValueError, match="immutable"):
        campaign().write_campaign_scenarios(changed, output)


def test_main_freeze_refuses_incomplete_or_changed_pilot_scope(tmp_path):
    settings, profiles, qualification, trace = protocol_fixture(tmp_path)
    rows = pilot_rows(settings, profiles, qualification, trace)
    with pytest.raises(ValueError, match=r"six|pilot"):
        campaign().freeze_campaign_protocol(
            profiles=profiles,
            qualification=qualification,
            pilot_rows=rows[:5],
            settings=settings,
        )
    rows[0]["runs"]["ewma"]["manifest"]["profile_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="scope"):
        campaign().freeze_campaign_protocol(
            profiles=profiles,
            qualification=qualification,
            pilot_rows=rows,
            settings=settings,
        )


@pytest.mark.parametrize("bad", [None, "seed", "rate", "scope", "hash"])
def test_public_campaign_binding_rejects_changes_before_vm_acquisition(tmp_path, bad):
    import yaml

    from nanolab.config import ScenarioConfig

    settings, profiles, qualification, trace = protocol_fixture(tmp_path)
    protocol = campaign().freeze_campaign_protocol(
        profiles=profiles, qualification=qualification, pilot_rows=[], settings=settings
    )
    files = campaign().write_campaign_scenarios(protocol, tmp_path / "pilot")
    config = ScenarioConfig.model_validate(yaml.safe_load(files[1].read_text()))
    settings = config.one_shot
    assert settings is not None and settings.experiment is not None
    assert settings.experiment.campaign_protocol is not None
    if bad == "seed":
        settings.seed += 1
    elif bad == "rate":
        settings.experiment.windows[0].rates["edge-1"]["f"] += 1
    elif bad == "scope":
        settings.nodes[0].cpus += 1
    elif bad == "hash":
        settings.experiment.campaign_protocol.sha256 = "a" * 64
    if bad:
        with pytest.raises(ValueError, match=r"frozen|hash"):
            campaign().read_campaign_binding(settings)
    else:
        binding = campaign().read_campaign_binding(settings)
        assert binding["trace"].sha256 == trace.sha256
        assert binding["block"]["seed"] == settings.seed
        assert binding["block"]["modes"] == protocol["blocks"][1]["modes"]


def test_replacements_require_frozen_external_failure_and_preserve_original():
    rows = [block(i) for i in range(12)]
    rows[0]["runs"].pop("ewma")
    rows[0]["invalidity"] = "external-technical"
    rows[0]["failureEvidence"] = {"cause": "host-reboot", "artifact": "host.json"}
    replacement = block(12)
    replacement["replacementFor"] = 0
    rows.append(replacement)
    protocol = {
        "plannedBlocks": 12,
        "replacements": [
            {"block": 12, "seed": 19, "modes": ["baseline", "oracle", "ewma"]},
            {"block": 13, "seed": 20, "modes": ["baseline", "oracle", "ewma"]},
        ],
    }
    result = campaign().paired_summary(rows, seed=7, protocol=protocol)
    assert result["complete"]
    assert result["excludedBlocks"][0]["block"] == 0
    assert result["replacedBlocks"] == [{"original": 0, "replacement": 12}]
    assert result["serialDiagnostics"]["blockOrder"] == list(range(1, 13))
    rows[0]["invalidity"] = "algorithm-associated"
    result = campaign().paired_summary(rows, seed=7, protocol=protocol)
    assert not result["complete"]
    assert result["primary"]["confidence95"] is None


def test_cell_totals_must_match_global_conservation():
    row = block()
    row["runs"]["baseline"]["evidence"]["planned"] += 1
    row["runs"]["baseline"]["evidence"]["emitted"] += 1
    row["runs"]["baseline"]["evidence"]["completed"] += 1
    with pytest.raises(ValueError, match="totals"):
        campaign().paired_summary([row] + [block(i) for i in range(1, 12)], seed=7)


def test_main_freeze_rejects_pilot_order_outside_registered_protocol(tmp_path):
    settings, profiles, qualification, trace = protocol_fixture(tmp_path)
    rows = pilot_rows(settings, profiles, qualification, trace)
    for run in rows[0]["runs"].values():
        run["manifest"]["modes"].reverse()
    with pytest.raises(ValueError, match="registered"):
        campaign().freeze_campaign_protocol(
            profiles=profiles,
            qualification=qualification,
            pilot_rows=rows,
            settings=settings,
        )
