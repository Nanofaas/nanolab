"""New live v2 runs have explicit version/session identity; archives stay v1."""

from datetime import UTC, datetime
from importlib.resources import files
from types import SimpleNamespace
from typing import cast

import pytest
import yaml

from nanolab.config.one_shot import OneShotConfig, ProtocolSettings
from nanolab.one_shot.client import NanoFaasOneShotClient
from nanolab.one_shot.models import CalibrationProfile
from nanolab.tasks.one_shot.qualification import configuration_for_node


def settings(version=1):
    return OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "protocolVersion": version,
            "nodes": [
                {
                    "id": node,
                    "kind": "cloud" if node == "cloud" else "edge",
                    "cpus": 1,
                    "memoryMiB": 1024,
                    "memoryCapacityMiB": 64,
                }
                for node in ["edge0", "edge1", "cloud"]
            ],
            "functions": {
                "f": {"input": {}, "cpu": 0.1, "memoryMiB": 64, "maxReplicas": 1}
            },
        }
    )


def test_v2_configuration_has_one_shared_session_and_grid():
    config = settings(2)
    anchor = datetime(2020, 1, 1, tzinfo=UTC)
    profile = SimpleNamespace(
        profile_id="profile",
        environment_fingerprint="sha256:" + "a" * 64,
        functions=[
            {
                "function": "f",
                "imageDigest": "sha256:" + "b" * 64,
                "inputHash": "sha256:" + "c" * 64,
            }
        ],
    )
    topology = SimpleNamespace(
        resources=SimpleNamespace(
            nodes={node.id: SimpleNamespace(config=node) for node in config.nodes}
        ),
        function_settings=config.functions,
    )
    client = SimpleNamespace(
        status=lambda: SimpleNamespace(catalog_generations={"f": 3})
    )
    rows = [
        configuration_for_node(
            topology,
            config,
            cast(CalibrationProfile, profile),
            node,
            client=cast(NanoFaasOneShotClient, client),
            cloud_uri="http://cloud:8080",
            period=360,
            lead=5,
            epsilon=0.1,
            protocol=ProtocolSettings(),
            anchor=anchor,
            session_id="session",
        )
        for node in ["edge0", "edge1"]
    ]
    assert all(row["schemaVersion"] == 2 for row in rows)
    assert all(row["sessionId"] == "session" for row in rows)
    assert all(row["anchor"] == anchor.isoformat() for row in rows)


def test_missing_explicit_version_is_legacy_and_invalid_versions_are_rejected():
    original = settings().model_dump(by_alias=True)
    original.pop("protocolVersion")
    assert OneShotConfig.model_validate(original).protocol_version == 1
    with pytest.raises(ValueError, match="protocolVersion"):
        settings(3)


@pytest.mark.parametrize("stage", ["calibration", "qualification", "experiment"])
def test_generic_presets_emit_logical_configuration_with_current_recipe_budgets(stage):
    root = files("nanolab").joinpath("assets/presets")
    data = yaml.safe_load(root.joinpath(f"scenarios/one-shot-{stage}.yaml").read_text())
    config = OneShotConfig.model_validate(data["oneShot"])
    qualification = OneShotConfig.model_validate(
        yaml.safe_load(
            root.joinpath("scenarios/one-shot-qualification.yaml").read_text()
        )["oneShot"]
    )
    timing = qualification.timing
    assert timing is not None
    function = next(iter(config.functions))
    profile = SimpleNamespace(
        profile_id="profile",
        environment_fingerprint="sha256:" + "a" * 64,
        functions=[
            {
                "function": function,
                "imageDigest": "sha256:" + "b" * 64,
                "inputHash": "sha256:" + "c" * 64,
            }
        ],
    )
    topology = SimpleNamespace(
        resources=SimpleNamespace(
            nodes={n.id: SimpleNamespace(config=n) for n in config.nodes}
        ),
        function_settings=config.functions,
    )
    client = SimpleNamespace(
        status=lambda: SimpleNamespace(catalog_generations={function: 1})
    )
    payload = configuration_for_node(
        topology,
        config,
        cast(CalibrationProfile, profile),
        "edge-0",
        client=cast(NanoFaasOneShotClient, client),
        cloud_uri="http://cloud:8080",
        period=timing.period_candidates[0],
        lead=timing.lead_seconds,
        epsilon=timing.epsilon,
        protocol=timing.protocol,
        anchor=datetime(2026, 11, 11, tzinfo=UTC),
        session_id="shared-session",
    )
    assert payload["schemaVersion"] == 2
    assert payload["sessionId"] == "shared-session"
    assert payload["negotiation"]["auctionBudget"] == "PT5.000000000S"
    assert payload["negotiation"]["peerTimeout"] == "PT0.500000000S"
    assert payload["negotiation"]["solverBudget"] == "PT1.000000000S"
    assert payload["preparationBudget"] == "PT5.000000000S"
    assert timing.protocol.auction_seconds < timing.lead_seconds
    assert timing.protocol.auction_seconds + timing.protocol.preparation_seconds <= (
        timing.epsilon * min(timing.period_candidates)
    )
