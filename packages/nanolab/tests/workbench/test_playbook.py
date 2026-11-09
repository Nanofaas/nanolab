from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nanolab.config.environment import MultipassSite
from nanolab.one_shot.infrastructure import NodeResource
from nanolab.one_shot.sites import SiteHost
from nanolab.tasks.resource_tasks import resource_of
from nanolab.tasks.vm.models import VmInfo, VmRequest
from nanolab.workbench import playbook
from nanolab.workbench.inventory import HostVars, Instance
from nanolab.workbench.playbook import WorkbenchHost, WorkbenchPlaybook

VARS = HostVars(instances=(Instance(name="prom", component="prometheus"),))
INSTALLED = resource_of("Install", lambda _i: Path("/coll"), lambda _i, _v: None)
VM = resource_of("vm", lambda _i: None, lambda _i, _v: None)


def run(host, tmp_path, monkeypatch, action: Any = "deploy"):
    captured = []
    monkeypatch.setattr(
        playbook,
        "run_bootstrap_operations",
        lambda provider, operations, *, role: captured.append(
            (provider, operations, role)
        ),
    )
    inputs = SimpleNamespace(
        resource=lambda r: (
            Path("/coll")
            if r is INSTALLED
            else VmInfo(name="n", host="10.0.0.5", user="ubuntu", home="/home/ubuntu")
        )
    )
    WorkbenchPlaybook(
        "t", host, action, VARS, vars_dir=tmp_path, installed=INSTALLED
    ).run(inputs)  # type: ignore[arg-type]
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
    assert operation.argv[2] == "10.0.0.5,"
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
