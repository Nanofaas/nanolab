import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from sonata_engine import Task, TaskInputs, TaskOutcome, Workflow

from nanolab.one_shot.infrastructure import NodeResource
from nanolab.tasks.resource_tasks import release_now, resource_of
from nanolab.tasks.vm.models import VmInfo, VmRequest
from nanolab.workbench import components, playbook
from nanolab.workbench.components import Deployed, collect_task, component_resource
from nanolab.workbench.inventory import Instance
from nanolab.workbench.playbook import WorkbenchHost

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
        playbook,
        "run_bootstrap_operations",
        lambda _p, operations, *, role: events.append(
            f"{operations[0].operation_id}:{operations[0].summary}"
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
        playbook,
        "run_bootstrap_operations",
        lambda _p, operations, *, role: captured.append(
            (operations[0].operation_id, operations[0].argv[2])
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
    assert captured == [("workbench.undeploy", "10.0.0.5," if node else "localhost,")]


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
