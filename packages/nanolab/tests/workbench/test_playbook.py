import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import Mock

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.ansible import AnsiblePlaybookTask
from sonata_tasks.shell import ShellExecutionResult

from nanolab.config.environment import MultipassSite
from nanolab.one_shot.infrastructure import NodeResource
from nanolab.one_shot.sites import SiteHost
from nanolab.tasks.resource_tasks import resource_of
from nanolab.tasks.vm.models import VmInfo, VmRequest
from nanolab.workbench.inventory import HostVars, Instance
from nanolab.workbench.playbook import WorkbenchHost, WorkbenchPlaybook
from tests.workbench.playbook_fakes import inventory_of

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
        AnsiblePlaybookTask, "run", lambda task, inputs: captured.append(task)
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
    task = run(WorkbenchHost("operator", None), tmp_path, monkeypatch)
    argv = task.argv[4:]
    assert argv[:4] == (sys.executable, "-m", "ansible.cli.playbook", "-i")
    assert (
        json.loads(Path(inventory_of(task)).read_text())["all"]["hosts"]["localhost"][
            "ansible_connection"
        ]
        == "local"
    )
    assert (
        argv[-1] == "/coll/ansible_collections/nanofaas/workbench/playbooks/deploy.yml"
    )
    path = tmp_path / "operator-deploy.json"
    assert f"workbench_vars={path}" in argv
    assert HostVars.model_validate_json(path.read_text()) == VARS
    assert task.options.env["ANSIBLE_COLLECTIONS_PATH"] == "/coll"
    assert task.options.env["ANSIBLE_CONFIG"].endswith("ansible_assets/ansible.cfg")


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
    task = run(WorkbenchHost("edge-1", _node()), tmp_path, monkeypatch, "collect")
    facts = json.loads(Path(inventory_of(task)).read_text())["all"]["hosts"]["edge-1"]
    assert facts["ansible_host"] == "10.0.0.5"
    assert facts["ansible_user"] == "ubuntu"
    assert task.argv[-1].endswith("playbooks/collect.yml")
    assert "edge-1" in inventory_of(task)
    assert (tmp_path / "edge-1-collect.json").is_file()


def test_site_node_goes_through_site(tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("x")
    site = MultipassSite(
        ssh="spark", address="10.9.9.9", architecture="arm64", vmSshKey=str(key)
    )
    task = run(WorkbenchHost("edge-1", _node(SiteHost(site))), tmp_path, monkeypatch)
    assert (
        "ProxyCommand="
        in json.loads(Path(inventory_of(task)).read_text())["all"]["hosts"]["edge-1"][
            "ansible_ssh_common_args"
        ]
    )
    assert task.options.env["ANSIBLE_COLLECTIONS_PATH"] == "/coll"


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
    task = run(WorkbenchHost("edge-1", node), tmp_path, monkeypatch)
    inventory = json.loads(Path(inventory_of(task)).read_text())
    facts = inventory["all"]["hosts"]["edge-1"]
    assert facts["ansible_host"] == "203.0.113.8"
    assert facts["ansible_user"] == "ubuntu"
    assert facts["ansible_private_key_file"] == str(tmp_path / "key")
    if cloud == "proxmox":
        assert facts["ansible_port"] == 2222


def test_sites_with_same_private_address_keep_distinct_aliases(tmp_path, monkeypatch):
    inventories = []
    for key, jump in (("edge-a", "site-a"), ("edge-b", "site-b")):
        site = MultipassSite(
            ssh=jump,
            address="10.9.9.9",
            architecture="arm64",
            vmSshKey=str(tmp_path / jump),
        )
        task = run(WorkbenchHost(key, _node(SiteHost(site))), tmp_path, monkeypatch)
        inventory = json.loads(Path(inventory_of(task)).read_text())
        assert inventory["all"]["hosts"][key]["ansible_host"] == "10.0.0.5"
        assert jump in inventory["all"]["hosts"][key]["ansible_ssh_common_args"]
        inventories.append(inventory_of(task))
    assert inventories[0] != inventories[1]


@pytest.mark.parametrize("alias", ["../escape", "a/b", "a\\b", ".."])
def test_workbench_host_rejects_unsafe_alias(alias):
    with pytest.raises(ValueError, match="alias"):
        WorkbenchHost(alias, _node())


def test_relative_vars_directory_is_resolved_before_ansible(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = run(WorkbenchHost("operator", None), Path("vars"), monkeypatch)
    assert f"workbench_vars={tmp_path / 'vars/operator-deploy.json'}" in task.argv


def test_connection_facts_are_resolved_without_rendering_a_command(tmp_path):
    node = _node()
    vm = VmInfo(name="n", host="10.0.0.5", user="resolved", home="/home/resolved")
    assert node.ansible_vars(vm)["ansible_host"] == vm.host
    assert node.ansible_vars(vm)["ansible_user"] == "resolved"


def test_site_key_and_jump_override_local_discovery(tmp_path, monkeypatch):
    site = MultipassSite(
        ssh="site-a", address="10.9.9.9", architecture="arm64", vmSshKey="~/site key"
    )
    node = _node(SiteHost(site))
    vm = VmInfo(name="n", host="10.0.0.5", user="ubuntu", home="/home/ubuntu")
    variables = node.ansible_vars(vm)
    assert variables["ansible_private_key_file"] == str(Path("~/site key").expanduser())
    assert "site-a" in variables["ansible_ssh_common_args"]
    assert "%%C" in variables["ansible_ssh_common_args"]


def test_playbook_runner_isolates_stdin_and_reports_failure(tmp_path, monkeypatch):
    from nanolab.tasks.infra.ansible import playbook_task

    seen = []

    class Shell:
        def run(self, argv, **kwargs):
            seen.append(argv)
            return ShellExecutionResult(
                command=argv, return_code=2, stdout="details", stderr="failed"
            )

    task = playbook_task(
        title="Deploy",
        playbook=tmp_path / "deploy.yml",
        inventory=tmp_path / "inventory.json",
        hosts={"localhost": {"ansible_connection": "local"}},
        extra_vars={},
        shell=Shell(),
    )
    with pytest.raises(
        RuntimeError,
        match=r"exit 2.*failed",
    ):
        task.run(TaskInputs._for_resource_values({}, ()))
    assert seen[0][:4] == ["sh", "-c", 'exec "$@" </dev/null', "sh"]
