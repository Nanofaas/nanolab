"""New live v2 runs have explicit version/session identity; archives stay v1."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

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
