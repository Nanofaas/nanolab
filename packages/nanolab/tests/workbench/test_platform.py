import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from sonata_engine import (
    JournalConfig,
    Task,
    TaskInputs,
    TaskOutcome,
    Workflow,
    release_retained,
)

from nanolab.config.one_shot import HostCpuPlacement, OneShotLogs
from nanolab.one_shot.infrastructure import NodeResource
from nanolab.tasks.resource_tasks import resource_of
from nanolab.tasks.vm.models import VmInfo, VmRequest
from nanolab.workbench import components, platform, playbook
from nanolab.workbench.components import Deployed
from nanolab.workbench.platform import one_shot_platform

NODES = ("edge-1", "cloud-1")
LOGS = OneShotLogs.model_validate(
    {
        "enabled": True,
        "endpoint": "http://10.0.0.1:3100/loki/api/v1/push",
        "backendOwned": True,
        "backendBind": "10.0.0.1",
    }
)
PLACEMENT = HostCpuPlacement.model_validate(
    {
        "nodes": {
            n: {"cpus": [i + 2], "maxFrequencyKHz": 1} for i, n in enumerate(NODES)
        },
        "operatorCpus": [0],
        "loggingCpus": [6, 7],
    }
)
SETTINGS = SimpleNamespace(
    observability=SimpleNamespace(logs=LOGS), host_cpu_placement=PLACEMENT
)
FORWARDS = {
    n: {"node-exporter": f"127.0.0.1:{19100 + i}", "cadvisor": f"127.0.0.1:{18089 + i}"}
    for i, n in enumerate(NODES)
}
SEEN: dict = {}  # deployed instance name -> its vars, as the fake saw them
VM = VmInfo(name="n", host="10.0.0.5", user="ubuntu", home="/home/ubuntu")


def _node(key: str) -> NodeResource:
    vm = resource_of(
        f"vm {key}",
        lambda _i: VM,
        lambda _i, _v: None,
        revive=lambda raw: VmInfo(**raw),
    )
    request = VmRequest(lifecycle="external", name=key, user="ubuntu", host="h")
    return NodeResource(
        SimpleNamespace(id=key, kind="edge"),  # type: ignore[arg-type]
        request,
        vm,
        SimpleNamespace(shell=None),
    )


def _build(tmp_path, monkeypatch, events, *, fail=None):
    """Build a platform whose playbooks and collect side effects only record."""
    vars_dir = tmp_path / "run" / "workbench" / "vars"
    SEEN.clear()

    def bootstrap(_provider, operations, *, role):
        action = operations[0].operation_id.removeprefix("workbench.")
        saved = json.loads((vars_dir / f"{role}-{action}.json").read_text())
        names = tuple(i["name"] for i in saved["instances"])
        if fail in names and action == "deploy":
            raise RuntimeError("deploy failed")
        events.append((role, action, names))
        if action == "collect" and names[0].startswith("log-collector-"):
            metrics = Path(saved["collect_dir"]) / names[0] / "metrics.txt"
            metrics.parent.mkdir(parents=True, exist_ok=True)
            metrics.write_text("")
        if action == "deploy":
            SEEN.update({i["name"]: i for i in saved["instances"]})

    monkeypatch.setattr(playbook, "run_bootstrap_operations", bootstrap)
    monkeypatch.setattr(components, "ensure_installed", lambda: Path("/coll"))
    monkeypatch.setattr(platform, "ensure_installed", lambda: Path("/coll"))
    monkeypatch.setattr(
        platform,
        "workbench_resource",
        lambda: resource_of("Install", lambda _i: Path("/coll"), lambda _i, _v: None),
    )
    record = lambda name: lambda *_a, **_k: events.append(("collect", name, ()))  # noqa: E731
    monkeypatch.setattr(platform, "export_vm_resources", record("vm-resources"))
    monkeypatch.setattr(platform, "verify_loki_ingestion", record("loki-ingestion"))
    monkeypatch.setattr(platform, "export_queue_events", lambda *_a: 0)
    monkeypatch.setattr(
        platform,
        "_inspect",
        lambda _host, name: {"container": name, "image": "sha256:i", "cpuset": None},
    )
    monkeypatch.setattr(
        platform.SupervisorClient, "status", lambda _self: {"state": "healthy"}
    )
    observability = tmp_path / "nanofaas" / "deploy" / "observability"
    observability.mkdir(parents=True, exist_ok=True)
    for name in ("fluent-bit.conf", "parsers.conf"):
        (observability / name).write_text(name)
    nodes = {key: _node(key) for key in NODES}
    return one_shot_platform(
        SimpleNamespace(nodes=nodes),  # type: ignore[arg-type]
        SETTINGS,  # type: ignore[arg-type]
        run_dir=tmp_path / "run",
        nanofaas_root=tmp_path / "nanofaas",
        forwards=FORWARDS,
        mode="ORACLE",
    )


class Consume(Task[None]):
    title = "consume"
    idempotent = False

    def __init__(self, events: list) -> None:
        self.events = events

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        del inputs
        self.events.append(("consume", "", ()))
        return TaskOutcome()


def _run(built, events) -> None:
    workflow = Workflow("platform-test")
    workflow.add(Consume(events), requires=(built.resource,))
    workflow.run()


DEPLOY_ORDER = [
    ("operator", "node-exporter-operator"),
    ("operator", "cadvisor-operator"),
    ("edge-1", "node-exporter-edge-1"),
    ("edge-1", "cadvisor-edge-1"),
    ("cloud-1", "node-exporter-cloud-1"),
    ("cloud-1", "cadvisor-cloud-1"),
    ("operator", "loki-operator"),
    ("edge-1", "log-collector-edge-1"),
    ("cloud-1", "log-collector-cloud-1"),
    ("operator", "prometheus-operator"),
    ("operator", "supervisor-operator"),
]


def _of(events, action):
    return [(host, names[0]) for host, act, names in events if act == action]


def test_member_order_and_reverse_release(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    assert _of(events, "deploy") == DEPLOY_ORDER
    assert _of(events, "undeploy") == DEPLOY_ORDER[::-1]


def test_failed_platform_acquire_undeploys_earlier_members(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events, fail="prometheus-operator")
    with pytest.raises(Exception, match="deploy failed"):
        _run(built, events)
    assert _of(events, "deploy") == DEPLOY_ORDER[:9]
    assert _of(events, "undeploy") == DEPLOY_ORDER[:9][::-1]
    assert ("consume", "", ()) not in events


def test_platform_release_from_journal(tmp_path, monkeypatch):
    """A later process releases every member on its recorded host."""
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    members = {
        title: asdict(
            Deployed(host, title, "x", None, None if host == "operator" else asdict(VM))
        )
        for host, title in (
            (host, f"Deploy {name} on {host}") for host, name in DEPLOY_ORDER
        )
    }
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps(
            {
                "kind": "retained",
                "resource": built.resource.title,
                "value": members,
                "order": 0,
            }
        )
        + "\n"
    )
    released = release_retained(
        {built.resource.title: built.resource}, JournalConfig(journal)
    )
    assert released == (built.resource.title,)
    assert _of(events, "undeploy") == DEPLOY_ORDER[::-1]


def test_supervisor_targets_cover_every_node(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    config = SEEN["supervisor-operator"]["config"]
    targets = config["targets"]
    hosts = ("operator", *NODES)
    for host in hosts:
        for job in ("node", "cadvisor"):
            assert targets[f"up.{job}.{host}"]["params"] == {
                "job": job,
                "labels": {"node": host},
            }
    for node in NODES:
        assert targets[f"running.control-plane.{node}"]["params"] == {
            "name": "nanolab-one-shot-control-plane",
            "node": node,
        }
        assert targets[f"running.log-collector.{node}"]["params"] == {
            "name": f"wb-log-collector-{node}",
            "node": node,
        }
    assert targets["swap.operator"] == {
        "kind": "swap_quiet",
        "params": {
            "node": "operator",
            "maxSwapPages": 256,
            "minAvailableBytes": 8 * 1024**3,
        },
        "toleratedSeconds": 0,
    }
    assert len(targets) == 2 * len(hosts) + 2 * len(NODES) + 1
    assert config["prometheus_url"] == "http://127.0.0.1:9090"
    jobs = SEEN["prometheus-operator"]["config"]["jobs"]
    assert jobs["node"] == [
        {"target": "127.0.0.1:9100", "labels": {"node": "operator"}},
        {"target": "127.0.0.1:19100", "labels": {"node": "edge-1"}},
        {"target": "127.0.0.1:19101", "labels": {"node": "cloud-1"}},
    ]
    assert [t["target"] for t in jobs["cadvisor"]] == [
        "127.0.0.1:8089",
        "127.0.0.1:18089",
        "127.0.0.1:18090",
    ]


def test_cpusets_follow_host_cpu_placement(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    assert SEEN["loki-operator"]["cpuset"] == "6,7"
    for node in NODES:
        collector = SEEN[f"log-collector-{node}"]
        assert "cpuset" not in collector  # host CPU ids mean nothing in the VM
        assert collector["config"]["cpus"] == LOGS.cpu
        assert collector["config"]["memory_mib"] == LOGS.memory_mib
    assert all(
        "cpuset" not in SEEN[name]
        for name in SEEN
        if not name.startswith(("loki-", "log-collector-"))
    )


def test_collect_runs_before_undeploy(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    actions = [(host, action) for host, action, _ in events]
    first_undeploy = actions.index(("operator", "undeploy"))
    collects = actions[actions.index(("consume", "")) + 1 : first_undeploy]
    assert collects == [
        ("operator", "collect"),
        ("collect", "vm-resources"),
        ("collect", "loki-ingestion"),
        ("edge-1", "collect"),
        ("cloud-1", "collect"),
    ]
    status = tmp_path / "run" / "workbench" / "supervisor-status.json"
    assert json.loads(status.read_text()) == {"state": "healthy"}
    for node in NODES:
        status = json.loads(
            (tmp_path / "run/logging" / node / "status.json").read_text()
        )
        assert status["identity"]["image"] == "sha256:i"
        assert status["identity"]["container"] == f"wb-log-collector-{node}"
        assert status["collection"]["status"] == "unverified"
    placement = json.loads((tmp_path / "run/logging-host-placement.json").read_text())
    assert placement["container"] == "wb-loki-operator"
