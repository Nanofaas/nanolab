import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import Mock

import pytest
from sonata_engine import TaskInputs

from nanolab.config.environment import MultipassSite
from nanolab.one_shot.infrastructure import NodeResource
from nanolab.one_shot.sites import SiteHost
from nanolab.tasks.resource_tasks import resource_of
from nanolab.tasks.vm.models import VmInfo, VmRequest
from nanolab.workbench import playbook
from nanolab.workbench.inventory import HostVars, Instance
from nanolab.workbench.playbook import WorkbenchHost, WorkbenchPlaybook

VARS = HostVars(instances=(Instance(name="prom", component="prometheus"),))
VM = resource_of("vm", lambda _i: None, lambda _i, _v: None)


def run(
    host: WorkbenchHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: Literal["deploy", "collect", "undeploy"] = "deploy",
):
    captured = []
    monkeypatch.setattr(
        playbook,
        "run_bootstrap_operations",
        lambda provider, operations, *, role: captured.append(
            (provider, operations, role)
        ),
    )
    vm = (
        None
        if host.node is None
        else VmInfo(name="n", host="10.0.0.5", user="ubuntu", home="/home/ubuntu")
    )
    WorkbenchPlaybook(
        "t", host, action, VARS, vars_dir=tmp_path, collections=Path("/coll"), vm=vm
    ).run(TaskInputs._for_resource_values({}, ()))
    return captured[0]


def test_operator_host_runs_locally(tmp_path, monkeypatch):
    _, (operation,), _ = run(WorkbenchHost("operator", None), tmp_path, monkeypatch)
    argv = operation.argv
    assert argv[:3] == ("ansible-playbook", "-i", "localhost,")
    assert (
        argv[-1] == "/coll/ansible_collections/nanofaas/workbench/playbooks/deploy.yml"
    )
    path = tmp_path / "operator-deploy.json"
    assert f"workbench_vars={path}" in argv
    assert HostVars.model_validate_json(path.read_text()) == VARS
    assert operation.env["ANSIBLE_COLLECTIONS_PATH"] == "/coll"
    assert operation.env["ANSIBLE_CONFIG"].endswith("ansible_assets/ansible.cfg")


def _node(site_host=None):
    request = VmRequest(lifecycle="external", name="nl-x", user="ubuntu", host="h")
    return NodeResource(
        SimpleNamespace(id="edge-1"),  # type: ignore[arg-type]
        request,
        VM,  # type: ignore[arg-type]
        SimpleNamespace(shell=None),
        site_host=site_host,
    )


def test_node_targets_its_vm(tmp_path, monkeypatch):
    _, (operation,), role = run(
        WorkbenchHost("edge-1", _node()), tmp_path, monkeypatch, "collect"
    )
    assert json.loads(Path(operation.argv[2]).read_text()) == {
        "all": {"hosts": {"edge-1": {"ansible_host": "10.0.0.5"}}}
    }
    assert operation.argv[-1].endswith("playbooks/collect.yml")
    assert role == "edge-1"
    assert (tmp_path / "edge-1-collect.json").is_file()


def test_site_node_goes_through_site(tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("x")
    site = MultipassSite(
        ssh="spark", address="10.9.9.9", architecture="arm64", vmSshKey=str(key)
    )
    _, (operation,), _ = run(
        WorkbenchHost("edge-1", _node(SiteHost(site))), tmp_path, monkeypatch
    )
    assert "ProxyCommand=" in operation.env["ANSIBLE_SSH_COMMON_ARGS"]
    assert operation.env["ANSIBLE_COLLECTIONS_PATH"] == "/coll"


@pytest.mark.parametrize("cloud", ["azure", "proxmox"])
def test_stable_alias_preserves_provider_endpoint_and_ssh(tmp_path, monkeypatch, cloud):
    from sonata_tasks.vm.azure import AzureVmProvider
    from sonata_tasks.vm.proxmox import ProxmoxVmProvider

    provider = Mock(spec=AzureVmProvider if cloud == "azure" else ProxmoxVmProvider)
    if cloud == "azure":
        provider.connection_host.return_value = "203.0.113.8"
    else:
        provider.ssh_endpoint.return_value = ("203.0.113.8", 2222)
    provider.ssh_private_key_path.return_value = tmp_path / "key"
    node = replace(_node(), provider=provider)
    _, (operation,), _ = run(WorkbenchHost("edge-1", node), tmp_path, monkeypatch)
    inventory = json.loads(Path(operation.argv[2]).read_text())
    assert inventory["all"]["hosts"] == {"edge-1": {"ansible_host": "203.0.113.8"}}
    assert operation.argv[operation.argv.index("-u") + 1] == "ubuntu"
    assert operation.argv[operation.argv.index("--private-key") + 1] == str(
        tmp_path / "key"
    )
    if cloud == "proxmox":
        assert "ansible_port=2222" in operation.argv


def test_sites_with_same_private_address_keep_distinct_aliases(tmp_path, monkeypatch):
    inventories = []
    for key, jump in (("edge-a", "site-a"), ("edge-b", "site-b")):
        site = MultipassSite(
            ssh=jump,
            address="10.9.9.9",
            architecture="arm64",
            vmSshKey=str(tmp_path / jump),
        )
        _, (operation,), _ = run(
            WorkbenchHost(key, _node(SiteHost(site))), tmp_path, monkeypatch
        )
        inventory = json.loads(Path(operation.argv[2]).read_text())
        assert inventory["all"]["hosts"] == {key: {"ansible_host": "10.0.0.5"}}
        assert jump in operation.env["ANSIBLE_SSH_COMMON_ARGS"]
        inventories.append(operation.argv[2])
    assert inventories[0] != inventories[1]


@pytest.mark.parametrize("alias", ["../escape", "a/b", "a\\b", ".."])
def test_workbench_host_rejects_unsafe_alias(alias):
    with pytest.raises(ValueError, match="alias"):
        WorkbenchHost(alias, _node())
