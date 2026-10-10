"""Keep measured demand and immutable calibration bindings local to each node."""

import json
from importlib import import_module

import pytest

from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot.contracts import contract_asset
from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash


def fixture(tmp_path):
    settings = {
        "provider": "multipass",
        "purpose": "scientific-experiment",
        "nodes": [
            {
                "id": "edge-1",
                "kind": "edge",
                "functionLimits": {
                    "f": {"cpu": 0.35, "memoryMiB": 128, "maxReplicas": 2}
                },
            },
            {"id": "edge-2", "kind": "edge"},
            {"id": "cloud", "kind": "cloud"},
        ],
        "functions": {"f": {"input": {}}},
    }
    index = {
        "schemaVersion": 1,
        "artifactType": "node-profile-set",
        "provider": "multipass",
        "purpose": "scientific-experiment",
        "environmentFingerprint": "sha256:" + "a" * 64,
        "nodes": {},
        "nodeFingerprints": {},
    }
    for number, (node, demand, quota) in enumerate(
        [("edge-1", 0.3, 0.35), ("edge-2", 0.1, 1), ("cloud", 0.1, 1)]
    ):
        doc = json.loads(contract_asset("examples/synthetic-profile.json").read_bytes())
        doc.update(
            profileId=node,
            synthetic=False,
            provider="multipass",
            purpose="scientific-experiment",
            environmentFingerprint="sha256:" + str(number + 1) * 64,
        )
        row = doc["functions"][0]
        row.update(
            serviceSeconds=demand,
            cpuQuota=quota,
            backend="container-local",
            inputHash="sha256:" + content_hash(canonical_bytes({})),
        )
        row["statistics"]["meanSeconds"] = demand
        row["validity"]["maxReplicas"] = 2
        path = tmp_path / (node + ".json")
        path.write_bytes(canonical_bytes(doc))
        index["nodes"][node] = {
            "path": path.name,
            "sha256": content_hash(path.read_bytes()),
        }
        index["nodeFingerprints"][node] = doc["environmentFingerprint"]
    capacity = tmp_path / "capacity.json"
    capacity.write_bytes(
        canonical_bytes(
            {
                "schemaVersion": 1,
                "artifactType": "measured-node-capacity",
                "environmentFingerprint": index["environmentFingerprint"],
                "capacities": {"edge-1": 5.0, "edge-2": 16.0, "cloud": 16.0},
            }
        )
    )
    index["capacity"] = {
        "path": capacity.name,
        "sha256": content_hash(capacity.read_bytes()),
    }
    return settings, index


def read(tmp_path, settings, index):
    path = tmp_path / "profiles.json"
    path.write_bytes(canonical_bytes(index))
    settings["profile"] = {"path": path, "sha256": content_hash(path.read_bytes())}
    return import_module("nanolab.one_shot.profiles").read_profiles(
        OneShotConfig.model_validate(settings)
    )


def test_node_demands_are_never_averaged(tmp_path):
    settings, index = fixture(tmp_path)
    profiles = read(tmp_path, settings, index)
    assert profiles.by_node["edge-1"].functions[0]["serviceSeconds"] == 0.30
    assert profiles.by_node["edge-2"].functions[0]["serviceSeconds"] == 0.10
    assert profiles.capacities == {"edge-1": 5.0, "edge-2": 16.0, "cloud": 16.0}


@pytest.mark.parametrize(
    "bad",
    [
        "hash",
        "missing",
        "swap",
        "limits",
        "fingerprint",
        "escape",
        "absolute",
        "capacity",
    ],
)
def test_invalid_bindings_fail_before_vm_acquisition(tmp_path, bad):
    settings, index = fixture(tmp_path)
    if bad == "hash":
        (tmp_path / "edge-1.json").write_text("{}")
    if bad == "missing":
        index["nodes"].pop("edge-1")
    if bad == "swap":
        index["nodes"]["edge-1"], index["nodes"]["edge-2"] = (
            index["nodes"]["edge-2"],
            index["nodes"]["edge-1"],
        )
    if bad == "limits":
        settings["nodes"][0]["functionLimits"]["f"]["cpu"] = 0.4
    if bad == "fingerprint":
        index["nodeFingerprints"]["edge-1"] = "sha256:" + "9" * 64
    if bad == "escape":
        index["nodes"]["edge-1"]["path"] = "../edge-1.json"
    if bad == "absolute":
        index["nodes"]["edge-1"]["path"] = str(tmp_path / "edge-1.json")
    if bad == "capacity":
        index["capacity"]["sha256"] = "0" * 64
    with pytest.raises(
        ValueError, match=r"hash|scope|fingerprint|resource|relative|dossier"
    ):
        read(tmp_path, settings, index)


def test_single_profile_is_rejected_for_heterogeneous_limits(tmp_path):
    settings, index = fixture(tmp_path)
    settings["profile"] = {
        "path": tmp_path / "edge-1.json",
        "sha256": index["nodes"]["edge-1"]["sha256"],
    }
    with pytest.raises(ValueError, match="homogeneous"):
        import_module("nanolab.one_shot.profiles").read_profiles(
            OneShotConfig.model_validate(settings)
        )


def test_legacy_profile_keeps_capacity_unknown(tmp_path):
    settings, index = fixture(tmp_path)
    settings["nodes"][0].pop("functionLimits")
    settings["profile"] = {
        "path": tmp_path / "edge-2.json",
        "sha256": index["nodes"]["edge-2"]["sha256"],
    }
    profiles = import_module("nanolab.one_shot.profiles").read_profiles(
        OneShotConfig.model_validate(settings)
    )
    assert profiles.capacities is None
    assert profiles.by_node["edge-1"].functions[0]["serviceSeconds"] == 0.1


def test_publish_node_profiles_preserves_local_demand_and_immutable_bytes(tmp_path):
    from types import SimpleNamespace
    from typing import Any, cast

    from nanolab.tasks.one_shot.artifacts import append_observation
    from nanolab.tasks.one_shot.calibration import PublishCalibrationTask

    data, index = fixture(tmp_path)
    model = OneShotConfig.model_validate(data)
    samples = {}
    for node, demand in [("edge-1", 0.3), ("edge-2", 0.1), ("cloud", 0.1)]:
        samples[node + "/f"] = [
            {
                "node": node,
                "function": "f",
                "state": "complete",
                "occupancySeconds": demand,
            }
            for _ in range(24)
        ]
        for sample in samples[node + "/f"]:
            append_observation(tmp_path / "samples.jsonl", sample)
    value = {
        "samples": samples,
        "statistics": {},
        "source": {"revision": "a" * 40},
        "fingerprint": index["environmentFingerprint"],
        "nodeFingerprints": index["nodeFingerprints"],
        "nodeEnvironments": {
            node: {
                "host": "test",
                "vm": "multipass",
                "os": "linux",
                "architecture": "aarch64",
                "cpu": "test",
            }
            for node in index["nodes"]
        },
        "measuredCapacity": {"edge-1": 5.0, "edge-2": 16.0, "cloud": 16.0},
        "environment": {
            "host": "test",
            "vm": "multipass",
            "os": "linux",
            "architecture": "aarch64",
            "cpu": "test",
        },
    }
    built = SimpleNamespace(
        function=lambda *_: SimpleNamespace(
            image=SimpleNamespace(id="sha256:" + "b" * 64)
        )
    )
    topology = SimpleNamespace(
        function_settings=model.functions,
        function_aliases={"f": "f"},
        distribution="distribution",
    )
    inputs = cast(
        Any, SimpleNamespace(upstream=lambda: value, resource=lambda _: built)
    )
    task = PublishCalibrationTask(
        topology, model, tmp_path / "published", clock=cast(Any, "clock")
    )
    # The run directory is made by the plan; samples remain the immutable input.
    task.run_dir.mkdir()
    (task.run_dir / "samples.jsonl").write_bytes(
        (tmp_path / "samples.jsonl").read_bytes()
    )
    task.run(inputs)
    reference = task.run_dir / "profile.json"
    data["profile"] = {
        "path": reference,
        "sha256": content_hash(reference.read_bytes()),
    }
    profiles = import_module("nanolab.one_shot.profiles").read_profiles(
        OneShotConfig.model_validate(data)
    )
    assert profiles.by_node["edge-1"].functions[0]["serviceSeconds"] == pytest.approx(
        0.3
    )
    assert profiles.by_node["cloud"].functions[0]["serviceSeconds"] == pytest.approx(
        0.1
    )
    original = reference.read_bytes()
    task.run(inputs)
    assert reference.read_bytes() == original
    value["measuredCapacity"]["cloud"] = 17.0
    with pytest.raises(ValueError, match="immutable"):
        task.run(inputs)
