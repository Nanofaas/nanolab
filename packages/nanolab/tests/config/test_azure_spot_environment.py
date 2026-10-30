"""The campaign provisions its own Spot pair through the existing provider."""

from pathlib import Path

import pytest
import yaml
from azure_vm import AzureClient
from azure_vm.testing import CommandResult, FakeBackend

from nanolab.application.vm_provider import (
    provider_for_environment,
    vm_request_for_role,
)
from nanolab.config.environment import EnvironmentConfig
from nanolab.tasks.provisioning.environment import ProvisionedRole, provision_roles

ENVIRONMENT = Path(
    "packages/nanolab/src/nanolab/assets/presets/environments/"
    "azure-comparison-spot.yaml.example"
)


def test_campaign_creates_both_spot_vms_through_managed_provider(tmp_path, monkeypatch):
    environment = EnvironmentConfig.model_validate(
        yaml.safe_load(ENVIRONMENT.read_text())
    )
    provider = provider_for_environment(environment, tmp_path)
    backend = FakeBackend()
    backend.set_default(CommandResult(args=[], returncode=0, stdout="{}", stderr=""))
    client = AzureClient(
        resource_group="test-rg",
        location="westeurope",
        work_dir=str(tmp_path / "sdk"),
        backend=backend,
    )
    monkeypatch.setattr(provider, "_client", lambda request: client)
    monkeypatch.setattr(provider, "_exists_in_azure", lambda request: False)
    monkeypatch.setattr(provider, "connection_host", lambda request: "203.0.113.10")
    monkeypatch.setattr(
        provider,
        "_az_json",
        lambda command: {
            "priority": "Spot",
            "evictionPolicy": "Deallocate",
            "maxPrice": -1,
        },
    )

    assert set(environment.roles) == {"stack", "loadgen"}
    roles = tuple(
        ProvisionedRole(
            role=role, request=vm_request_for_role(environment, role, loadtest=True)
        )
        for role in ("stack", "loadgen")
    )
    with provision_roles(provider, roles, repo_root=tmp_path, assets_root=tmp_path):
        for entry in roles:
            request = entry.request
            assert request.host is None
            assert request.azure_open_ports is None
            assert request.name is not None
            hcl = (tmp_path / "sdk" / request.name / "main.tf").read_text()
            assert 'priority = "Spot"' in hcl
            assert 'eviction_policy = "Deallocate"' in hcl
            assert "max_bid_price = -1" in hcl
    assert backend.calls.count(["tofu", "destroy", "-auto-approve"]) == 2


@pytest.mark.parametrize(
    "settings",
    [
        {"priority": "Spot", "max_bid_price": -0.5},
        {"priority": "Spot", "max_bid_price": float("inf")},
        {"priority": "Spot", "max_bid_price": True},
        {"priority": "Regular", "eviction_policy": "Deallocate"},
    ],
)
def test_bad_spot_configuration_fails_before_provisioning(settings):
    with pytest.raises(ValueError, match=r"Spot|azure"):
        vm_request_for_role(
            EnvironmentConfig.model_validate(
                {
                    "provider": "azure",
                    "azure": {
                        "resource_group": "rg",
                        "location": "westeurope",
                        **settings,
                    },
                }
            ),
            "stack",
        )
