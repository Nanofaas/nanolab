import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from sonata_engine import Task, TaskInputs, TaskOutcome, Workflow
from sonata_tasks.ansible import AnsiblePlaybookTask

from nanolab.one_shot.infrastructure import NodeResource
from nanolab.tasks.resource_tasks import release_now, resource_of
from nanolab.tasks.vm.models import VmInfo, VmRequest
from nanolab.workbench import components
from nanolab.workbench.components import Deployed, collect_task, component_resource
from nanolab.workbench.inventory import Instance
from nanolab.workbench.playbook import WorkbenchHost
from tests.workbench.playbook_fakes import action_of, host_of, inventory_of

INSTALLED = resource_of("Install", lambda _i: Path("/coll"), lambda _i, _v: None)
HOST = WorkbenchHost("operator", None)
PROM = Instance(name="prom", component="prometheus")


class Consume(Task[None]):
    title = "consume"
    idempotent = False

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        del inputs
        self.events.append("consume")
        return TaskOutcome()


def _record(monkeypatch, events: list[str]) -> None:
    monkeypatch.setattr(components, "ensure_installed", lambda: Path("/coll"))
    monkeypatch.setattr(
        AnsiblePlaybookTask,
        "run",
        lambda task, inputs: events.append(
            f"{('workbench.' + action_of(task))}:{task.title}"
        ),
    )


def _resource(tmp_path):
    return component_resource(
        HOST, PROM, url="http://x:9090", vars_dir=tmp_path, installed=INSTALLED
    )


def _run(tmp_path, monkeypatch, *, keep=False):
    events: list[str] = []
    _record(monkeypatch, events)
    resource = _resource(tmp_path)
    workflow = Workflow("wb-test")
    workflow.add(Consume(events), requires=(resource,))
    workflow.keep = keep
    workflow.run()
    return events, resource


def test_component_resource_deploys_then_undeploys(tmp_path, monkeypatch):
    events, _ = _run(tmp_path, monkeypatch)
    assert [e.split(":")[0] for e in events] == [
        "workbench.deploy",
        "consume",
        "workbench.undeploy",
    ]


def test_undeploy_runs_when_kept(tmp_path, monkeypatch):
    events, resource = _run(tmp_path, monkeypatch, keep=True)
    assert resource.always_release
    assert events[-1].startswith("workbench.undeploy")


def _node() -> WorkbenchHost:
    vm = resource_of(
        "vm",
        lambda _i: VmInfo(name="n", host="h", user="u", home="/h"),
        lambda _i, _v: None,
        revive=lambda raw: VmInfo(**raw),
    )
    request = VmRequest(lifecycle="external", name="nl-x", user="ubuntu", host="h")
    node = NodeResource(
        SimpleNamespace(id="edge-1"),  # type: ignore[arg-type]
        request,
        vm,
        SimpleNamespace(shell=None),
    )
    return WorkbenchHost("edge-1", node)


@pytest.mark.parametrize("node", [False, True])
def test_release_from_journal_needs_no_live_resources(tmp_path, monkeypatch, node):
    """A later-process teardown runs with empty inputs and a revived value."""
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        AnsiblePlaybookTask,
        "run",
        lambda task, inputs: captured.append(
            (("workbench." + action_of(task)), inventory_of(task))
        ),
    )
    monkeypatch.setattr(components, "ensure_installed", lambda: Path("/coll"))
    host = _node() if node else HOST
    resource = component_resource(
        host, PROM, url=None, vars_dir=tmp_path, installed=INSTALLED
    )
    vm = VmInfo(name="n", host="10.0.0.5", user="ubuntu", home="/home/ubuntu")
    value = Deployed(host.key, "prom", "prometheus", None, asdict(vm) if node else None)
    assert resource.revive is not None
    revived = resource.revive(json.loads(json.dumps(asdict(value))))
    assert revived == value
    release_now(resource, TaskInputs._for_resource_values({}, ()), revived)
    expected = str(tmp_path / f"{host.key}-undeploy-inventory.json")
    assert captured == [("workbench.undeploy", expected)]
    if node:
        facts = json.loads(Path(expected).read_text())["all"]["hosts"]["edge-1"]
        assert facts["ansible_host"] == "10.0.0.5"
        assert facts["ansible_user"] == "ubuntu"


def test_titles_name_instance_and_host(tmp_path, monkeypatch):
    events, resource = _run(tmp_path, monkeypatch)
    assert events[0] == "workbench.deploy:Deploy prom on operator"
    assert resource.acquire.title == "Deploy prom on operator"
    assert resource.release_title == "Release Deploy prom on operator"
    assert resource.release.title == "Undeploy prom on operator"


def test_collect_task_runs_collect(tmp_path, monkeypatch):
    events: list[str] = []
    _record(monkeypatch, events)
    task = collect_task(HOST, (PROM,), Path("/runs/x"), tmp_path, INSTALLED)
    task.run(
        TaskInputs._for_resource_values({id(INSTALLED): Path("/coll")}, (INSTALLED,))
    )
    assert events == ["workbench.collect:Collect operator"]
    saved = json.loads((tmp_path / "operator-collect.json").read_text())
    assert saved["collect_dir"] == "/runs/x"


def test_config_from_inputs_is_deployed_and_recorded(tmp_path, monkeypatch):
    """Config resolved at acquire reaches the vars; undeploy reads it back."""
    _record(monkeypatch, [])
    saved: list[dict] = []
    monkeypatch.setattr(
        AnsiblePlaybookTask,
        "run",
        lambda task, inputs: saved.append(
            json.loads((tmp_path / f"operator-{action_of(task)}.json").read_text())[
                "instances"
            ][0]["config"]
        ),
    )
    resource = component_resource(
        HOST,
        PROM.model_copy(update={"config": {"port": 9090}}),
        url=None,
        vars_dir=tmp_path,
        installed=INSTALLED,
        config_from_inputs=lambda inputs: {"jobs": inputs.resource(INSTALLED).name},
    )
    outcome = resource.acquire.run(
        TaskInputs._for_resource_values({id(INSTALLED): Path("/coll")}, (INSTALLED,))
    )
    value = outcome.value
    assert value is not None
    assert value.config == {"jobs": "coll"}
    release_now(resource, TaskInputs._for_resource_values({}, ()), value)
    assert saved == [{"port": 9090, "jobs": "coll"}] * 2


@pytest.mark.parametrize("undeploy_fails", [False, True])
def test_a_failed_deploy_undeploys_best_effort(tmp_path, monkeypatch, undeploy_fails):
    from nanolab.tasks.resource_tasks import acquire_now

    events: list[str] = []
    monkeypatch.setattr(components, "ensure_installed", lambda: Path("/coll"))

    def bootstrap(task, inputs):
        role = host_of(task)
        del role
        action = "workbench." + action_of(task)
        events.append(action)
        if action == "workbench.deploy" or undeploy_fails:
            raise RuntimeError(f"{action} failed")

    monkeypatch.setattr(AnsiblePlaybookTask, "run", bootstrap)
    inputs = TaskInputs({id(INSTALLED): Path("/coll")}, {id(INSTALLED)})
    with pytest.raises(RuntimeError, match=r"workbench\.deploy failed") as caught:
        acquire_now(_resource(tmp_path), inputs)
    assert events == ["workbench.deploy", "workbench.undeploy"]
    notes = getattr(caught.value, "__notes__", [])
    assert any("undeploy" in note for note in notes) is undeploy_fails


@pytest.mark.parametrize("observed", ["0-1", "2-3", ""])
def test_deploy_refuses_incorrect_operator_cpuset(tmp_path, monkeypatch, observed):
    import subprocess

    events = []
    _record(monkeypatch, events)
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=observed)
    )
    resource = component_resource(
        HOST,
        PROM.model_copy(update={"cpuset": "0,1"}),
        url=None,
        vars_dir=tmp_path,
        installed=None,
    )
    workflow = Workflow("placement")
    workflow.add(Consume(events), requires=(resource,))
    if observed == "0-1":
        workflow.run()
        assert "consume" in events
    else:
        with pytest.raises(ValueError, match="CPU placement"):
            workflow.run()
        assert "consume" not in events
        assert any(e.startswith("workbench.undeploy:") for e in events)


def test_deployment_window_includes_container_startup(tmp_path, monkeypatch):
    from nanolab.tasks.resource_tasks import acquire_now

    clock = [1.0]
    _record(monkeypatch, [])
    monkeypatch.setattr(components.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        AnsiblePlaybookTask, "run", lambda *_: clock.__setitem__(0, 2.0)
    )
    value = acquire_now(
        _resource(tmp_path),
        TaskInputs._for_resources({INSTALLED: Path("/coll")}, {INSTALLED}),
    )
    assert value.deployed_at == 1.0
