"""A selected faasd requires its own Azure VM and bounded ingress."""

import pytest

from nanolab.application.experiment_02 import require_experiment_02_environment
from nanolab.config import EnvironmentConfig


def environment(roles):
    return EnvironmentConfig.model_validate(
        {
            "provider": "azure",
            "roles": {role: {"name": role} for role in roles},
            "azure": {
                "resource_group": "test-rg",
                "location": "westeurope",
                "operator_source_cidr": "203.0.113.1/32",
                "image_urn": "test:ubuntu:server:pinned-version",
            },
        }
    )


def test_faasd_requires_a_dedicated_role_without_changing_the_existing_pair():
    pair = environment(("stack", "loadgen"))
    require_experiment_02_environment(pair, ("fn",))
    with pytest.raises(ValueError, match="faasd"):
        require_experiment_02_environment(pair, ("faasd",))
    require_experiment_02_environment(
        environment(("stack", "loadgen", "faasd")), ("faasd",)
    )


def test_unselected_faasd_role_is_not_silently_provisioned():
    with pytest.raises(ValueError, match="roles"):
        require_experiment_02_environment(
            environment(("stack", "loadgen", "faasd")), ("fn",)
        )


def test_faasd_refuses_a_shared_vm_and_missing_ingress_policy():
    config = environment(("stack", "loadgen", "faasd"))
    config.roles["faasd"].name = config.roles["stack"].name
    with pytest.raises(ValueError, match="distinct"):
        require_experiment_02_environment(config, ("faasd",))
    config.roles["faasd"].name = "faasd"
    assert config.azure is not None
    config.azure.operator_source_cidr = None
    with pytest.raises(ValueError, match="CIDR"):
        require_experiment_02_environment(config, ("faasd",))
