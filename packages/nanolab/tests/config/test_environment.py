from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanolab.config.environment import EnvironmentConfig, RoleTarget


def test_local_environment_defaults_stack_to_host() -> None:
    config = EnvironmentConfig(provider="local")

    assert config.target("stack") == config.target("host")


def test_external_environment_requires_stack_host() -> None:
    with pytest.raises(ValidationError, match="stack host is required"):
        EnvironmentConfig(provider="external")


def test_external_environment_preserves_distinct_stack_and_loadgen() -> None:
    config = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {
                "stack": {"host": "stack.example", "user": "stack-user"},
                "loadgen": {"host": "load.example", "user": "load-user"},
            },
        }
    )

    assert config.target("stack").host == "stack.example"
    assert config.target("loadgen").host == "load.example"


def test_multipass_environment_uses_role_names() -> None:
    config = EnvironmentConfig.model_validate(
        {
            "provider": "multipass",
            "roles": {
                "stack": {"name": "nanofaas-stack"},
                "loadgen": {"name": "nanofaas-loadgen"},
            },
        }
    )

    assert config.target("stack").name == "nanofaas-stack"
    assert config.target("loadgen").name == "nanofaas-loadgen"


def test_multipass_environment_requires_stack_name() -> None:
    with pytest.raises(ValidationError, match="stack name is required"):
        EnvironmentConfig(provider="multipass")


def test_azure_environment_requires_provider_configuration() -> None:
    with pytest.raises(ValidationError, match="azure configuration is required"):
        EnvironmentConfig(provider="azure")


def test_proxmox_environment_requires_provider_configuration() -> None:
    with pytest.raises(ValidationError, match="proxmox configuration is required"):
        EnvironmentConfig(provider="proxmox")


def test_rejects_legacy_prefect_configuration() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EnvironmentConfig.model_validate(
            {"provider": "local", "prefect": {"enabled": True}}
        )


def test_remote_home_defaults_to_the_users_home() -> None:
    assert RoleTarget(user="ubuntu").remote_home == "/home/ubuntu"


def test_remote_home_is_slash_root_for_root() -> None:
    assert RoleTarget(user="root").remote_home == "/root"


def test_an_explicit_home_wins() -> None:
    assert (
        RoleTarget(user="ubuntu", home="/srv/nanofaas").remote_home == "/srv/nanofaas"
    )


def test_hybrid_cloud_uses_azure_and_keeps_edge_and_generator_multipass() -> None:
    from nanolab.application.vm_provider import vm_request_for_role

    environment = EnvironmentConfig.model_validate(
        {
            "provider": "multipass",
            "roles": {
                "stack": {"name": "edge"},
                "loadgen": {"name": "loadgen"},
                "cloud": {"name": "cloud", "provider": "azure"},
            },
            "azure": {
                "resource_group": "rg",
                "location": "westeurope",
                "priority": "Spot",
            },
        }
    )
    assert environment.role_provider("cloud") == "azure"
    assert environment.role_provider("stack") == "multipass"
    cloud = vm_request_for_role(environment, "cloud", loadtest=True)
    assert cloud.lifecycle == "azure"
    assert cloud.azure_priority == "Spot"
    assert cloud.user == "azureuser"
    assert cloud.azure_open_ports is None
    assert vm_request_for_role(environment, "loadgen").lifecycle == "multipass"


@pytest.mark.parametrize(
    ("provider", "role", "azure"),
    [
        ("multipass", "stack", {"resource_group": "rg", "location": "westeurope"}),
        ("multipass", "loadgen", {"resource_group": "rg", "location": "westeurope"}),
        ("azure", "cloud", {"resource_group": "rg", "location": "westeurope"}),
        ("multipass", "cloud", None),
    ],
)
def test_hybrid_provider_override_rejects_unsupported_topologies(
    provider, role, azure
) -> None:
    with pytest.raises(ValidationError, match=r"cloud|azure"):
        EnvironmentConfig.model_validate(
            {
                "provider": provider,
                "roles": {
                    "stack": {"name": "edge"},
                    role: {"name": role, "provider": "azure"},
                },
                "azure": azure,
            }
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {
                "roles": {
                    "stack": {"name": "edge"},
                    "cloud": {"name": "EDGE"},
                    "loadgen": {"name": "loadgen"},
                }
            },
            "distinct",
        ),
        (
            {
                "roles": {
                    "stack": {"name": "edge"},
                    "cloud": {},
                    "loadgen": {"name": "loadgen"},
                }
            },
            "explicit machine",
        ),
        (
            {
                "provider": "local",
                "roles": {
                    "stack": {"name": "edge"},
                    "cloud": {"name": "cloud"},
                    "loadgen": {"name": "loadgen"},
                },
            },
            "three distinct",
        ),
        (
            {
                "roles": {
                    "stack": {"name": "edge"},
                    "cloud": {
                        "name": "cloud",
                        "provider": "azure",
                        "host": "20.30.40.50",
                    },
                    "loadgen": {"name": "loadgen"},
                }
            },
            "not precreated",
        ),
        (
            {
                "azure": {
                    "resource_group": "rg",
                    "location": "westeurope",
                    "priority": "Regular",
                }
            },
            "Spot",
        ),
        (
            {
                "azure": {
                    "resource_group": "rg",
                    "location": "westeurope",
                    "priority": "Spot",
                    "operator_source_cidr": "0.0.0.0/0",
                }
            },
            "restricted network",
        ),
    ],
)
def test_experiment_environment_refuses_ambiguous_or_unsafe_placement(
    change, message
) -> None:
    from nanolab.config.environment import require_experiment_environment

    data = {
        "provider": "multipass",
        "roles": {
            "stack": {"name": "edge"},
            "loadgen": {"name": "loadgen"},
            "cloud": {"name": "cloud", "provider": "azure"},
        },
        "azure": {"resource_group": "rg", "location": "westeurope", "priority": "Spot"},
    }
    with pytest.raises(ValueError, match=message):
        require_experiment_environment(EnvironmentConfig.model_validate(data | change))
