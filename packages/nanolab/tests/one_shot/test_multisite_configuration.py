"""Multi-site one-shot configuration: remote Multipass sites, Azure, per-arch images."""

import copy
import hashlib

import pytest
from pydantic import ValidationError

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.one_shot import OneShotConfig

AZURE = {
    "resource_group": "rg",
    "location": "westeurope",
    "priority": "Spot",
    "eviction_policy": "Delete",
    "max_bid_price": -1,
    "vm_size": "Standard_D8s_v5",
}


def environment(**overrides):
    data = {
        "provider": "multipass",
        "roles": {"stack": {"name": "one-shot"}},
        "sites": {
            "spark-35": {
                "ssh": "149.132.179.35",
                "address": "149.132.179.35",
                "architecture": "arm64",
                "multipass": "~/.local/share/nanolab/multipass-fixed/bin/multipass",
            }
        },
        "placement": {
            "edge-1": "spark-35",
            "edge-2": "spark-35",
            "cloud": "azure",
        },
        "azure": AZURE,
    }
    data.update(overrides)
    return data


def test_sites_and_placement_are_parsed():
    config = EnvironmentConfig.model_validate(environment())
    assert config.sites["spark-35"].address == "149.132.179.35"
    assert config.site_of("edge-1") == "spark-35"
    assert config.site_of("cloud") == "azure"
    assert config.multisite


def test_an_environment_without_sites_keeps_the_local_behaviour():
    config = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    assert not config.multisite
    assert config.site_of("edge-1") is None


def test_placement_must_name_a_declared_site():
    data = environment(placement={"edge-1": "spark-175", "cloud": "azure"})
    with pytest.raises(ValidationError, match="unknown site"):
        EnvironmentConfig.model_validate(data)


def test_azure_placement_requires_the_azure_block():
    data = environment()
    del data["azure"]
    with pytest.raises(ValidationError, match="azure configuration"):
        EnvironmentConfig.model_validate(data)


def test_azure_is_reserved_and_cannot_be_a_site_name():
    data = environment()
    data["sites"]["azure"] = data["sites"]["spark-35"]
    with pytest.raises(ValidationError, match="reserved"):
        EnvironmentConfig.model_validate(data)


def test_sites_are_only_meaningful_for_multipass():
    data = environment(provider="azure")
    with pytest.raises(ValidationError, match="multipass"):
        EnvironmentConfig.model_validate(data)


def test_site_address_must_be_an_ipv4_address():
    data = environment()
    data["sites"]["spark-35"]["address"] = "spark.example"
    with pytest.raises(ValidationError, match="IPv4"):
        EnvironmentConfig.model_validate(data)


def test_placement_without_sites_is_rejected():
    data = environment(sites={}, placement={"cloud": "azure"})
    with pytest.raises(ValidationError, match="sites"):
        EnvironmentConfig.model_validate(data)


LOGGING = {"cpus": [6, 7]}


def test_operator_logging_is_parsed_with_its_defaults():
    config = EnvironmentConfig.model_validate(environment(logging=LOGGING))
    assert config.operator_logging is not None
    assert config.operator_logging.port == 43100
    assert config.operator_logging.cpus == [6, 7]


def test_operator_logging_requires_a_placement():
    data = {
        "provider": "multipass",
        "roles": {"stack": {"name": "one-shot"}},
        "logging": LOGGING,
    }
    with pytest.raises(ValidationError, match="sites and placement"):
        EnvironmentConfig.model_validate(data)


def test_operator_logging_needs_cpus():
    with pytest.raises(ValidationError):
        EnvironmentConfig.model_validate(environment(logging={"cpus": []}))


@pytest.mark.parametrize("field", ["lokiBinary", "lokiSha256"])
def test_the_removed_loki_binary_fields_are_named(field):
    logging = {**LOGGING, field: "/opt/loki"}
    with pytest.raises(ValidationError, match=f"{field} was removed"):
        EnvironmentConfig.model_validate(environment(logging=logging))


def one_shot(**overrides):
    data = {
        "provider": "multipass",
        "purpose": "workflow-validation",
        "nodes": [
            {"id": "edge-1", "kind": "edge", "cpus": 1},
            {"id": "edge-2", "kind": "edge", "cpus": 1},
            {"id": "cloud", "kind": "cloud", "cpus": 2},
        ],
        "functions": {"f": {"input": {}, "cpu": 0.1}},
        "hostCpuPlacement": {
            "nodes": {
                "edge-1": {"cpus": [0], "maxFrequencyKHz": 2808000},
                "edge-2": {"cpus": [1], "maxFrequencyKHz": 2808000},
            },
            "operatorCpus": [4],
        },
    }
    data.update(overrides)
    return data


def test_host_placement_may_omit_only_the_cloud():
    config = OneShotConfig.model_validate(one_shot())
    assert config.host_cpu_placement is not None
    assert set(config.host_cpu_placement.nodes) == {"edge-1", "edge-2"}


def test_host_placement_still_rejects_a_missing_edge():
    data = one_shot()
    del data["hostCpuPlacement"]["nodes"]["edge-2"]
    with pytest.raises(ValidationError, match="coverage"):
        OneShotConfig.model_validate(data)


def artifact(tmp_path, name, content=b"{}"):
    path = tmp_path / name
    path.write_bytes(content)
    return {"path": str(path), "sha256": hashlib.sha256(content).hexdigest()}


def test_runtime_distributions_are_keyed_by_architecture(tmp_path):
    data = one_shot(
        runtimeDistributions={
            "arm64": artifact(tmp_path, "arm64.json"),
            "amd64": artifact(tmp_path, "amd64.json"),
        }
    )
    config = OneShotConfig.model_validate(data)
    assert config.runtime_distributions is not None
    assert set(config.runtime_distributions) == {"arm64", "amd64"}


def test_single_and_per_architecture_distributions_are_exclusive(tmp_path):
    data = one_shot(
        runtimeDistribution=artifact(tmp_path, "single.json"),
        runtimeDistributions={"arm64": artifact(tmp_path, "arm64.json")},
    )
    with pytest.raises(ValidationError, match="exclusive"):
        OneShotConfig.model_validate(data)


def test_unknown_architecture_is_rejected(tmp_path):
    data = one_shot(runtimeDistributions={"riscv64": artifact(tmp_path, "r.json")})
    with pytest.raises(ValidationError):
        OneShotConfig.model_validate(copy.deepcopy(data))
