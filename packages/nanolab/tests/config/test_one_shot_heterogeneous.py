"""Heterogeneous budgets must reach node registration without changing inputs."""

import pytest

from nanolab.config import one_shot as config
from nanolab.one_shot import infrastructure


def settings():
    return {
        "provider": "multipass",
        "purpose": "scientific-experiment",
        "nodes": [
            {
                "id": "edge-1",
                "kind": "edge",
                "cpus": 1,
                "functionLimits": {
                    "f": {"cpu": 0.35, "memoryMiB": 256, "maxReplicas": 1}
                },
            },
            {
                "id": "edge-2",
                "kind": "edge",
                "cpus": 1,
                "functionLimits": {
                    "f": {"cpu": 0.5, "memoryMiB": 256, "maxReplicas": 1}
                },
            },
            {
                "id": "cloud",
                "kind": "cloud",
                "cpus": 8,
                "memoryCapacityMiB": 1792,
                "functionLimits": {"f": {"cpu": 1, "memoryMiB": 256, "maxReplicas": 7}},
            },
        ],
        "functions": {"f": {"input": {"seed": 7}, "memoryMiB": 256}},
    }


def test_effective_limits_are_node_specific():
    model = config.OneShotConfig.model_validate(settings())
    edge = config.resolve_node_functions(model, "edge-1")["f"]
    cloud = config.resolve_node_functions(model, "cloud")["f"]
    assert (edge.cpu, edge.memory_mib, edge.max_replicas) == (0.35, 256, 1)
    assert (cloud.cpu, cloud.memory_mib, cloud.max_replicas) == (1, 256, 7)
    assert edge.input == cloud.input == {"seed": 7}
    assert model.functions["f"].max_replicas == 2


def test_replica_pool_cannot_exceed_cpu_budget():
    data = settings()
    data["nodes"][0].update(
        cpus=2,
        memoryCapacityMiB=768,
        functionLimits={"f": {"cpu": 1, "memoryMiB": 256, "maxReplicas": 3}},
    )
    with pytest.raises(ValueError, match="CPU"):
        config.OneShotConfig.model_validate(data)


def test_unknown_function_override_is_rejected():
    data = settings()
    data["nodes"][0]["functionLimits"] = {
        "unknown": {"cpu": 0.35, "memoryMiB": 256, "maxReplicas": 1}
    }
    with pytest.raises(ValueError, match="unknown function"):
        config.OneShotConfig.model_validate(data)


def test_legacy_preset_resolves_identical_limits():
    data = settings()
    data["purpose"] = "workflow-validation"
    for node in data["nodes"]:
        node.pop("functionLimits")
        node.update(cpus=2, memoryCapacityMiB=256)
    model = config.OneShotConfig.model_validate(data)
    assert config.resolve_node_functions(
        model, "edge-1"
    ) == config.resolve_node_functions(model, "cloud")


def test_host_budget_accounts_for_external_vms():
    model = config.OneShotConfig.model_validate(settings())
    with pytest.raises(ValueError, match="CPU"):
        infrastructure.host_budget(
            model,
            host_cpus=12,
            available_memory_mib=100000,
            external_cpus=4,
            reserve_cpus=1,
        )
    assert (
        infrastructure.host_budget(
            model,
            host_cpus=20,
            available_memory_mib=100000,
            external_cpus=4,
            reserve_cpus=1,
        )["cpuRequired"]
        == 15
    )


def test_window_preserves_multiple_epochs():
    window = config.ExperimentWindow.model_validate(
        {"id": "hotspot", "rates": {"edge-1": {"f": 10}}, "epochs": 6}
    )
    assert window.epochs == 6


def test_registration_uses_effective_node_limits(tmp_path, monkeypatch):
    from sonata_engine import Workflow

    from nanolab.config import EnvironmentConfig, ScenarioConfig
    from nanolab.plans.one_shot_common import add_one_shot_platforms

    data = settings()
    data["functions"] = {"one-shot-workload-rust": data["functions"].pop("f")}
    for node in data["nodes"]:
        node["functionLimits"] = {
            "one-shot-workload-rust": node["functionLimits"].pop("f")
        }
    scenario = ScenarioConfig.model_validate(
        {
            "workflow": "one-shot-calibration",
            "backend": "container",
            "functions": ["one-shot-workload-rust"],
            "oneShot": data,
        }
    )
    (tmp_path / "functions/rust/one-shot-workload").mkdir(parents=True)
    monkeypatch.setenv("NANOFAAS_ROOT", str(tmp_path))
    monkeypatch.setattr(infrastructure, "host_capacity", lambda: (20, 100000))
    monkeypatch.setattr(infrastructure, "external_vm_cpus", lambda: 4)
    topology = add_one_shot_platforms(
        Workflow("test"),
        scenario,
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
        ),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
    )
    edge, cloud = (
        topology.platforms["edge-1"].functions[0],
        topology.platforms["cloud"].functions[0],
    )
    # Registration Resource stores the request in its acquire closure, while the
    # resolved topology is the runtime consumer's public configuration boundary.
    assert topology.node_function_settings["edge-1"]["one-shot-workload"].cpu == 0.35
    assert (
        topology.node_function_settings["cloud"]["one-shot-workload"].max_replicas == 7
    )
    assert edge is not cloud
