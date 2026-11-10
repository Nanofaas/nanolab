import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from sonata_engine import (
    JournalConfig,
    Members,
    Task,
    TaskInputs,
    TaskOutcome,
    Workflow,
    release_retained,
)

from nanolab.config.environment import OperatorLogging
from nanolab.config.one_shot import HostCpuPlacement, OneShotLogs
from nanolab.one_shot.infrastructure import NodeResource
from nanolab.tasks.one_shot import logging as one_shot_logging
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
DRAINED = (
    "fluentbit_input_records_total 3\n"
    "fluentbit_output_proc_records_total 3\n"
    "fluentbit_output_dropped_records_total 0\n"
)
PENDING = DRAINED.replace("proc_records_total 3", "proc_records_total 1")


def _node(key: str, events: list) -> NodeResource:
    vm = resource_of(
        f"vm {key}",
        lambda _i: VM,
        lambda _i, _v: None,
        revive=lambda raw: VmInfo(**raw),
    )
    request = VmRequest(lifecycle="external", name=key, user="ubuntu", host="h")

    def exec_argv(_request, argv, remote_dir=None):
        del remote_dir
        if argv[:2] == ("docker", "stop"):
            events.append((key, "stop", (argv[-1],)))
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    return NodeResource(
        SimpleNamespace(id=key, kind="edge"),  # type: ignore[arg-type]
        request,
        vm,
        SimpleNamespace(shell=None, exec_argv=exec_argv),
    )


def _build(
    tmp_path,
    monkeypatch,
    events,
    *,
    fail=None,
    logs=LOGS,
    forwards=FORWARDS,
    metrics=None,
    operator_logging=None,
    log_endpoints=None,
    bootstraps=None,
    nodes=None,
):
    """Build a platform whose playbooks and collect side effects only record."""
    vars_dir = tmp_path / "run" / "workbench" / "vars"
    SEEN.clear()
    metrics = metrics or {}

    def bootstrap(_provider, operations, *, role):
        action = operations[0].operation_id.removeprefix("workbench.")
        saved = json.loads((vars_dir / f"{role}-{action}.json").read_text())
        names = tuple(i["name"] for i in saved["instances"])
        events.append((role, action, names))
        if fail in names and action in ("deploy", "collect"):
            raise RuntimeError(f"{action} failed")
        if action == "collect" and names[0].startswith("log-collector-"):
            path = Path(saved["collect_dir"]) / names[0] / "metrics.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(metrics.get(role, DRAINED))
        if action == "collect" and names[0].startswith("prometheus-"):
            path = Path(saved["collect_dir"]) / names[0] / "prometheus-snapshot.tar.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"snapshot")
        if action == "deploy":
            SEEN.update({i["name"]: i for i in saved["instances"]})

    def archive(node, source, path):
        events.append(("collect", "archive", (node.config.id, source)))
        path.write_bytes(b"tar")

    def failing_loki(**_kwargs):
        raise RuntimeError("no Loki in tests")

    monkeypatch.setenv("NANOLAB_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setattr(playbook, "run_bootstrap_operations", bootstrap)
    monkeypatch.setattr(components, "ensure_installed", lambda: Path("/coll"))
    monkeypatch.setattr(platform, "ensure_installed", lambda: Path("/coll"))
    monkeypatch.setattr(
        platform,
        "workbench_resource",
        lambda: resource_of("Install", lambda _i: Path("/coll"), lambda _i, _v: None),
    )
    record = lambda name: lambda *_a, **k: events.append(("collect", name, k))  # noqa: E731
    monkeypatch.setattr(platform, "export_vm_resources", record("vm-resources"))
    monkeypatch.setattr(platform, "verify_loki_ingestion", record("loki-ingestion"))
    monkeypatch.setattr(platform, "archive_node_dir", archive)
    monkeypatch.setattr(
        platform,
        "_as_root",
        lambda path, *argv: events.append(("operator", argv[0], path)),
    )
    monkeypatch.setattr(
        platform,
        "check_operator",
        lambda ports: events.append(("operator", "preflight", tuple(ports))),
    )
    monkeypatch.setattr(platform, "_loki_log", lambda _name: "loki says hi\n")
    monkeypatch.setattr(platform, "_operator_container_exists", lambda _name: False)
    monkeypatch.setattr(
        platform,
        "_pull_loadgen",
        lambda collections: events.append(("operator", "pull", (str(collections),))),
    )
    monkeypatch.setattr(one_shot_logging, "httpx", SimpleNamespace(Client=failing_loki))
    monkeypatch.setattr(
        platform,
        "time",
        SimpleNamespace(time=time.time, time_ns=time.time_ns, sleep=lambda _s: None),
    )
    monkeypatch.setattr(
        platform,
        "_inspect",
        lambda _host, name: {
            "container": name,
            "image": "sha256:i",
            "cpuset": None,
            "nanoCpus": round(LOGS.cpu * 1e9),
            "memoryBytes": LOGS.memory_mib * 1024 * 1024,
        },
    )

    def status(_self):
        events.append(("collect", "supervisor", ()))
        return {"state": "healthy"}

    monkeypatch.setattr(platform.SupervisorClient, "status", status)
    observability = tmp_path / "nanofaas" / "deploy" / "observability"
    observability.mkdir(parents=True, exist_ok=True)
    for name in ("fluent-bit.conf", "parsers.conf"):
        (observability / name).write_text(name)
    (observability / "loki.yaml").write_text(
        "auth_enabled: false\nserver:\n  http_listen_port: 3100\n"
    )
    nodes = nodes or {key: _node(key, events) for key in NODES}
    settings = SimpleNamespace(
        observability=SimpleNamespace(logs=logs), host_cpu_placement=PLACEMENT
    )
    return one_shot_platform(
        SimpleNamespace(nodes=nodes),  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        run_dir=tmp_path / "run",
        nanofaas_root=tmp_path / "nanofaas",
        forwards=forwards,
        mode="ORACLE",
        operator_logging=operator_logging,
        log_endpoints=log_endpoints,
        bootstraps=bootstraps,
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


def _deploy_order(tmp_path):
    return [
        ("operator", _op(tmp_path, "node-exporter")),
        ("operator", _op(tmp_path, "cadvisor")),
        ("edge-1", "node-exporter-edge-1"),
        ("edge-1", "cadvisor-edge-1"),
        ("cloud-1", "node-exporter-cloud-1"),
        ("cloud-1", "cadvisor-cloud-1"),
        ("operator", _op(tmp_path, "loki")),
        ("edge-1", "log-collector-edge-1"),
        ("cloud-1", "log-collector-cloud-1"),
        ("operator", _op(tmp_path, "prometheus")),
        ("operator", _op(tmp_path, "supervisor")),
    ]


def _op(tmp_path, component):
    """Name an operator instance: run-unique, as two runs share the host."""
    run = hashlib.sha256(str((tmp_path / "run").resolve()).encode()).hexdigest()
    return f"{component}-operator-{run[:8]}"


def _of(events, action):
    return [(host, names[0]) for host, act, names in events if act == action]


def test_member_order_and_reverse_release(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    assert _of(events, "deploy") == _deploy_order(tmp_path)
    assert _of(events, "undeploy") == _deploy_order(tmp_path)[::-1]


def test_failed_platform_acquire_undeploys_earlier_members(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events, fail=_op(tmp_path, "prometheus"))
    with pytest.raises(Exception, match="deploy failed"):
        _run(built, events)
    assert _of(events, "deploy") == _deploy_order(tmp_path)[:10]  # the last one failed
    # The failed member cleans itself up first, then the others in reverse.
    assert _of(events, "undeploy") == _deploy_order(tmp_path)[:10][::-1]
    assert ("consume", "", ()) not in events


def test_platform_release_from_journal(tmp_path, monkeypatch):
    """A later process releases every member on its recorded host."""
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    members: dict[str, Any] = {
        title: asdict(
            Deployed(host, title, "x", None, None if host == "operator" else asdict(VM))
        )
        for host, title in (
            (host, f"Deploy {name} on {host}") for host, name in _deploy_order(tmp_path)
        )
    }
    members["Own the Loki store"] = None
    members["Own the Prometheus store"] = None
    members["Check the operator prerequisites"] = None
    members["Pull the loadgen image"] = None
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
    assert _of(events, "undeploy") == _deploy_order(tmp_path)[::-1]


def test_supervisor_targets_cover_every_node(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    _run(built, events)
    config = SEEN[_op(tmp_path, "supervisor")]["config"]
    assert config["port"] == 8099
    assert config["prometheus_url"] == "http://127.0.0.1:9090"
    targets = built.targets
    # A control plane starts after the supervisor: its targets would fault
    # (sticky) before it exists, so they are registered once it is up.
    assert config["targets"] == {
        name: target
        for name, target in targets.items()
        if not name.startswith("running.control-plane.")
    }
    hosts = ("operator", *NODES)
    for host in hosts:
        for job in ("node", "cadvisor"):
            assert targets[f"up.{job}.{host}"]["params"] == {
                "job": job,
                "labels": {"node": host},
            }
    for node in NODES:
        assert targets[f"running.control-plane.{node}"] == {
            "kind": "container_running",
            "params": {
                "name": "nanolab-one-shot-control-plane",
                "node": node,
                "staleSeconds": 30,
            },
            "toleratedSeconds": 0,
        }
        assert targets[f"running.log-collector.{node}"]["params"] == {
            "name": f"wb-log-collector-{node}",
            "node": node,
            "staleSeconds": 30,
        }
    # Swapping broke runs; memory headroom is checked before the run.
    assert targets["swap.operator"] == {
        "kind": "swap_quiet",
        "params": {"node": "operator", "maxSwapPages": 256, "minAvailableBytes": 0},
        "toleratedSeconds": 30,
    }
    assert targets["running.loki.operator"] == {
        "kind": "container_running",
        "params": {
            "name": "wb-" + _op(tmp_path, "loki"),
            "node": "operator",
            "staleSeconds": 30,
        },
        "toleratedSeconds": 0,
    }
    assert targets["ready.loki.operator"] == {
        "kind": "http_ok",
        "params": {"url": "http://10.0.0.1:3100/ready"},
        "toleratedSeconds": 0,
    }
    assert len(targets) == 2 * len(hosts) + 2 * len(NODES) + 3


def test_forwarded_nodes_are_scraped_through_their_forwards(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    jobs = SEEN[_op(tmp_path, "prometheus")]["config"]["jobs"]
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
    # A site's forwarders reach the exporters at the VM's address.
    for component in ("node-exporter", "cadvisor"):
        assert all(
            SEEN[f"{component}-{n}"]["config"]["bind"] == "0.0.0.0" for n in NODES
        )
        assert "bind" not in SEEN[_op(tmp_path, component)]["config"]
    # cAdvisor's 5 s default housekeeping aliases a 5 s irate.
    for host in ("operator", *NODES):
        name = _op(tmp_path, "cadvisor") if host == "operator" else f"cadvisor-{host}"
        assert SEEN[name]["config"]["housekeeping_interval"] == "1s"


def test_unforwarded_nodes_are_scraped_at_the_vm_address(tmp_path, monkeypatch):
    events: list = []
    forwards = {"edge-1": FORWARDS["edge-1"]}
    _run(_build(tmp_path, monkeypatch, events, forwards=forwards), events)
    jobs = SEEN[_op(tmp_path, "prometheus")]["config"]["jobs"]
    assert [t["target"] for t in jobs["node"]] == [
        "127.0.0.1:9100",
        "127.0.0.1:19100",
        "10.0.0.5:9100",
    ]
    assert [t["target"] for t in jobs["cadvisor"]][2] == "10.0.0.5:8089"
    assert SEEN["node-exporter-cloud-1"]["config"]["bind"] == "0.0.0.0"
    assert SEEN["cadvisor-cloud-1"]["config"]["bind"] == "0.0.0.0"


def test_cpusets_follow_host_cpu_placement(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    assert SEEN[_op(tmp_path, "loki")]["cpuset"] == "6,7"
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


COLLECT_ORDER = [
    # First, before any collector stops: its target would fault.
    ("collect", "supervisor"),
    ("operator", "collect"),  # Prometheus snapshot; vm-resources are in-workflow
    ("edge-1", "collect"),
    ("edge-1", "stop"),
    ("collect", "archive"),
    ("cloud-1", "collect"),
    ("cloud-1", "stop"),
    ("collect", "archive"),
    ("collect", "loki-ingestion"),
]


def _collects(events):
    actions = [(host, action) for host, action, _ in events]
    first_undeploy = actions.index(("operator", "undeploy"))
    return actions[actions.index(("consume", "")) + 1 : first_undeploy]


def test_collect_runs_before_undeploy(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    assert _collects(events) == COLLECT_ORDER
    run = tmp_path / "run"
    status = run / "workbench" / "supervisor-status.json"
    assert json.loads(status.read_text()) == {"state": "healthy"}
    placement = json.loads((run / "logging-host-placement.json").read_text())
    assert placement["container"] == "wb-" + _op(tmp_path, "loki")
    assert (run / "logging" / "backend.log").read_text() == "loki says hi\n"
    for node in NODES:
        directory = run / "logging" / node
        status = json.loads((directory / "status.json").read_text())
        assert status["identity"]["image"] == "sha256:i"
        assert status["identity"]["container"] == f"wb-log-collector-{node}"
        assert status["stateArchive"] == "verified"
        assert (directory / "collector-state.tar.gz").read_bytes() == b"tar"
        assert (directory / "collector-metrics.txt").read_text() == DRAINED
        assert "status.json" in json.loads((directory / "sha256.json").read_text())


def test_collectors_poll_until_drained(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events, metrics={"cloud-1": PENDING})
    _run(built, events)
    collects = [host for host, action in _collects(events) if action == "collect"]
    assert collects.count("edge-1") == 1
    assert collects.count("cloud-1") == 5  # today's bounded drain wait
    statuses = {
        node: json.loads(
            (tmp_path / "run" / "logging" / node / "status.json").read_text()
        )
        for node in NODES
    }
    assert statuses["edge-1"]["collection"]["status"] == "drained"
    assert statuses["cloud-1"]["collection"]["status"] == "pending"
    assert "collection delivery is pending" in statuses["cloud-1"]["issues"]


def test_a_failing_node_keeps_the_others_evidence(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    original = playbook.run_bootstrap_operations

    def flaky(provider, operations, *, role):
        if role == "edge-1" and operations[0].operation_id == "workbench.collect":
            events.append((role, "collect", ()))
            raise RuntimeError("edge-1 unreachable")
        return original(provider, operations, role=role)

    monkeypatch.setattr(playbook, "run_bootstrap_operations", flaky)
    _run(built, events)  # a failed poll is evidence, not a failed release
    logging_dir = tmp_path / "run" / "logging"
    edge = json.loads((logging_dir / "edge-1" / "status.json").read_text())
    cloud = json.loads((logging_dir / "cloud-1" / "status.json").read_text())
    assert edge["status"] == "partial"
    assert any("edge-1 unreachable" in issue for issue in edge["issues"])
    assert cloud["collection"]["status"] == "drained"
    assert ("collect", "supervisor") in _collects(events)


def test_logs_disabled_omits_logging(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events, logs=OneShotLogs())
    _run(built, events)
    assert built.loki is None
    assert built.collectors == {}
    assert not any(name.startswith(("loki-", "log-collector-")) for name in SEEN)
    targets = built.targets
    assert not any(name.startswith("running.log-collector.") for name in targets)
    assert "running.control-plane.edge-1" in targets
    assert _collects(events) == [
        ("collect", "supervisor"),
        ("operator", "collect"),
    ]
    built.export_vm_resources(tmp_path, datetime.now(UTC), datetime.now(UTC))
    (export,) = [k for _h, a, k in events if a == "vm-resources"]
    roles = export["nodes"]["edge-1"]
    assert roles.roles == {"nanolab-one-shot-control-plane": "control-plane"}
    assert roles.observers == ("wb-node-exporter-edge-1", "wb-cadvisor-edge-1")


def test_external_loki_is_refused(tmp_path, monkeypatch):
    logs = OneShotLogs.model_validate(
        {"enabled": True, "endpoint": "http://10.0.0.1:3100/loki/api/v1/push"}
    )
    with pytest.raises(ValueError, match="backendOwned"):
        _build(tmp_path, monkeypatch, [], logs=logs)


def test_loki_store_is_handed_back_after_loki(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    data = tmp_path / "run" / "logging" / "backend-data"
    data.mkdir(parents=True)
    _run(built, events)
    assert SEEN[_op(tmp_path, "loki")]["config"]["data_dir"] == str(data)
    undeploys = [names for _h, action, names in events if action == "undeploy"]
    after = [
        (host, action)
        for host, action, _ in events[
            events.index(("operator", "undeploy", (_op(tmp_path, "loki"),))) :
        ]
    ]
    assert after[1] == ("operator", "chown")
    assert undeploys[-1] == (_op(tmp_path, "node-exporter"),)


def test_loki_store_hashes_today_format(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    data = tmp_path / "run" / "logging" / "backend-data"
    (data / "chunks").mkdir(parents=True)
    (data / "chunks" / "a").write_text("a")
    _run(built, events)
    hashes = json.loads((data.parent / "backend-data-sha256.json").read_text())
    assert hashes == {
        "chunks/a": "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"
    }


def test_multi_site_loki_lives_under_backend(tmp_path, monkeypatch):
    operator_logging = OperatorLogging.model_validate({"port": 43100, "cpus": [5]})
    events: list = []
    _run(
        _build(tmp_path, monkeypatch, events, operator_logging=operator_logging),
        events,
    )
    loki = SEEN[_op(tmp_path, "loki")]
    backend = tmp_path / "run" / "logging" / "backend"
    assert loki["config"]["data_dir"] == str(backend / "backend-data")
    assert loki["config"]["port"] == 43100
    assert loki["cpuset"] == "5"
    assert (backend / "backend.log").exists()
    assert (backend / "logging-host-placement.json").exists()


def test_loki_config_keeps_the_wal_settings(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    path = Path(SEEN[_op(tmp_path, "loki")]["config"]["config_file"])
    assert path.parent == tmp_path / "run" / "logging"
    config = yaml.safe_load(path.read_text())
    assert config["ingester"]["wal"] == {"enabled": True, "disk_full_threshold": 0}
    assert config["server"] == {"http_listen_port": 3100}


def test_prometheus_data_stays_out_of_the_run(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    run = _op(tmp_path, "x").removeprefix("x-operator-")
    tsdb = tmp_path / "workspace" / ".workbench" / run / "prometheus"
    assert SEEN[_op(tmp_path, "prometheus")]["config"]["data_dir"] == str(tsdb)


def test_the_tsdb_is_removed_once_its_snapshot_is_in_the_run(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    run = _op(tmp_path, "x").removeprefix("x-operator-")
    tsdb = tmp_path / "workspace" / ".workbench" / run / "prometheus"
    tsdb.mkdir(parents=True)
    _run(built, events)
    assert ("operator", "find", tsdb) in events  # as root: Prometheus is nobody
    assert events.index(("operator", "find", tsdb)) > events.index(
        ("operator", "undeploy", (_op(tmp_path, "prometheus"),))
    )
    assert not tsdb.parent.exists()


def test_the_tsdb_stays_while_prometheus_is_still_up(tmp_path, monkeypatch):
    """A failed undeploy leaves the container on the TSDB: nothing is deleted."""
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    asked: list[str] = []
    monkeypatch.setattr(
        platform, "_operator_container_exists", lambda name: asked.append(name) or True
    )
    run = _op(tmp_path, "x").removeprefix("x-operator-")
    tsdb = tmp_path / "workspace" / ".workbench" / run / "prometheus"
    tsdb.mkdir(parents=True)
    _run(built, events)
    assert asked == [f"wb-{_op(tmp_path, 'prometheus')}"]
    assert tsdb.is_dir()
    assert not any(action == "find" for _h, action, _p in events)


def test_the_tsdb_stays_without_a_snapshot(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events, fail=_op(tmp_path, "prometheus"))
    run = _op(tmp_path, "x").removeprefix("x-operator-")
    tsdb = tmp_path / "workspace" / ".workbench" / run / "prometheus"
    tsdb.mkdir(parents=True)
    with pytest.raises(Exception, match="deploy failed"):
        _run(built, events)
    assert tsdb.is_dir()
    assert not any(action == "find" for _h, action, _p in events)


def test_two_runs_never_share_an_operator_instance(tmp_path, monkeypatch):
    """Tearing down an old run's operator containers cannot match a live run's."""
    events: list = []
    _run(_build(tmp_path / "a", monkeypatch, events), events)
    first = {name for host, name in _of(events, "deploy") if host == "operator"}
    events.clear()
    _run(_build(tmp_path / "b", monkeypatch, events), events)
    second = {name for host, name in _of(events, "deploy") if host == "operator"}
    assert len(first) == len(second) == 5
    assert not first & second


def test_collectors_push_through_their_route(tmp_path, monkeypatch):
    """A route's push URL, known once it is open, configures the collector."""
    events: list = []
    route = resource_of(
        "route edge-1",
        lambda _i: "http://10.83.80.1:43100/loki/api/v1/push",
        lambda _i, _v: None,
    )
    built = _build(tmp_path, monkeypatch, events, log_endpoints={"edge-1": route})
    collector = built.collectors["edge-1"]
    assert route in collector.requires
    _run(built, events)
    assert SEEN["log-collector-edge-1"]["config"]["loki"] == {
        "host": "10.83.80.1",
        "port": 43100,
        "uri": "/loki/api/v1/push",
        "tls": "Off",
    }
    assert SEEN["log-collector-cloud-1"]["config"]["loki"]["host"] == "10.0.0.1"
    drains = [k for h, a, k in events if h == "edge-1" and a == "collect"]
    assert drains  # the drain poll runs with the resolved config too


def test_node_members_wait_for_the_node_bootstrap(tmp_path, monkeypatch):
    events: list = []
    bootstrap = resource_of(
        "bootstrap edge-1",
        lambda _i: events.append(("edge-1", "bootstrap", ())),
        lambda _i, _v: None,
    )
    built = _build(tmp_path, monkeypatch, events, bootstraps={"edge-1": bootstrap})
    assert bootstrap in built.resource.requires
    _run(built, events)
    actions = [(host, action) for host, action, _ in events]
    first_on_edge = next(
        i for i, (host, action) in enumerate(actions) if host == "edge-1"
    )
    assert actions[first_on_edge] == ("edge-1", "bootstrap")


def test_collector_identity_is_the_running_collector(tmp_path, monkeypatch):
    events: list = []
    commands: list = []
    nodes = {key: _node(key, events) for key in NODES}

    def command(argv, **_kwargs):
        commands.append(argv)
        return SimpleNamespace(stdout="json-file\n")

    monkeypatch.setattr(NodeResource, "command", lambda _self, argv: command(argv))
    built = _build(tmp_path, monkeypatch, events, nodes=nodes)
    identity = built.collector_identity("edge-1")
    assert identity == {
        "image": "sha256:i",
        "configSha256": {
            "fluent-bit.conf": hashlib.sha256(b"fluent-bit.conf").hexdigest(),
            "parsers.conf": hashlib.sha256(b"parsers.conf").hexdigest(),
        },
        "cpu": LOGS.cpu,
        "memoryMiB": LOGS.memory_mib,
        "bufferMiB": LOGS.buffer_mib,
    }
    commands.clear()
    monkeypatch.setattr(
        NodeResource,
        "command",
        lambda _self, argv: SimpleNamespace(stdout="journald\n"),
    )
    with pytest.raises(ValueError, match="json-file"):
        built.collector_identity("edge-1")
    monkeypatch.setattr(NodeResource, "command", lambda _self, argv: command(argv))
    monkeypatch.setattr(
        platform,
        "_inspect",
        lambda _host, name: {"image": "sha256:i", "nanoCpus": 1, "memoryBytes": 1},
    )
    with pytest.raises(ValueError, match="budget mismatch"):
        built.collector_identity("edge-1")


def test_a_custom_collector_image_is_refused(tmp_path, monkeypatch):
    logs = LOGS.model_copy(update={"image": "fluent/fluent-bit:9"})
    with pytest.raises(ValueError, match=r"logs\.image"):
        _build(tmp_path, monkeypatch, [], logs=logs)


def test_a_window_exports_with_the_run_roles(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    built.export_vm_resources(tmp_path / "window", start, end)
    ((_h, _a, export),) = [e for e in events if e[1] == "vm-resources"]
    assert export["start"] == start and export["end"] == end
    assert export["step_seconds"] == 5
    assert export["nodes"]["edge-1"].observers == (
        "wb-node-exporter-edge-1",
        "wb-cadvisor-edge-1",
        "wb-log-collector-edge-1",
    )


def test_the_loadgen_image_is_pulled_with_the_platform(tmp_path, monkeypatch):
    """A deploy after the anchor is captured must never wait on a pull."""
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    assert ("operator", "pull", ("/coll",)) in events
    assert events.index(("operator", "pull", ("/coll",))) < events.index(
        ("consume", "", ())
    )


def test_pull_loadgen_pulls_the_pinned_image(tmp_path, monkeypatch):
    defaults = tmp_path / "ansible_collections/nanofaas/workbench/roles/loadgen"
    (defaults / "defaults").mkdir(parents=True)
    (defaults / "defaults/main.yml").write_text("loadgen_image: grafana/k6@sha256:a\n")
    calls = []
    monkeypatch.setattr(
        platform.subprocess, "run", lambda argv, **_k: calls.append(argv)
    )
    platform._pull_loadgen(tmp_path)
    assert calls == [("docker", "pull", "grafana/k6@sha256:a")]


def test_a_recorded_supervisor_status_is_kept(tmp_path, monkeypatch):
    """Supervised runs record it at detach, before the targets are removed."""
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    status = tmp_path / "run" / "workbench" / "supervisor-status.json"
    status.parent.mkdir(parents=True)
    status.write_text('{"state": "fault"}\n')
    _run(built, events)
    assert ("collect", "supervisor", ()) not in events
    assert json.loads(status.read_text()) == {"state": "fault"}


def test_record_supervisor_status_writes_the_status(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    built.record_supervisor_status(
        Members(
            {
                built.supervisor.title: Deployed(
                    "operator", "s", "supervisor", "http://127.0.0.1:1"
                )
            }
        )
    )
    status = tmp_path / "run" / "workbench" / "supervisor-status.json"
    assert json.loads(status.read_text()) == {"state": "healthy"}


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired("docker", 60), OSError("no docker")]
)
def test_a_failed_loki_log_still_verifies_ingestion(tmp_path, monkeypatch, failure):
    events: list = []
    built = _build(tmp_path, monkeypatch, events)

    def broken(_name):
        raise failure

    monkeypatch.setattr(platform, "_loki_log", broken)
    _run(built, events)
    assert ("collect", "loki-ingestion") in _collects(events)
    log = (tmp_path / "run" / "logging" / "backend.log").read_text()
    assert str(failure) in log


def test_the_operator_prerequisites_are_checked_first(tmp_path, monkeypatch):
    events: list = []
    _run(_build(tmp_path, monkeypatch, events), events)
    preflight = [e for e in events if e[1] == "preflight"]
    assert preflight == [
        (
            "operator",
            "preflight",
            (
                ("127.0.0.1", 9090),
                ("127.0.0.1", 8099),
                ("127.0.0.1", 9100),
                ("127.0.0.1", 8089),
                ("10.0.0.1", 3100),
            ),
        )
    ]
    assert events.index(preflight[0]) < next(
        i for i, e in enumerate(events) if e[1] in ("pull", "deploy")
    )


def test_the_loki_log_reads_the_run_container(tmp_path, monkeypatch):
    events: list = []
    built = _build(tmp_path, monkeypatch, events)
    names = []
    monkeypatch.setattr(
        platform, "_loki_log", lambda name: names.append(name) or "log\n"
    )
    _run(built, events)
    assert names == ["wb-" + _op(tmp_path, "loki")]


@pytest.mark.parametrize(
    "failure", ["archive", "checksum", "stop", "export", "receipt", None]
)
def test_collector_archive_failure_preserves_owned_vm(tmp_path, monkeypatch, failure):
    from nanolab.config.one_shot import OneShotConfig
    from nanolab.one_shot.infrastructure import _owned_node

    events = []
    deleted = []
    config = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "nodes": [
                {"id": "edge-1", "kind": "edge"},
                {"id": "edge-2", "kind": "edge"},
                {"id": "cloud-1", "kind": "cloud"},
            ],
            "functions": {"f": {"input": {}}},
        }
    )
    nodes = {}
    for node in config.nodes:
        if node.id not in NODES:
            continue
        fake = _node(node.id, events).provider
        fake.vm_exists = lambda _r: False
        fake.ensure_running = lambda _r: SimpleNamespace(return_code=0)
        fake.connection_host = lambda _r: VM.host
        fake.teardown = lambda r: (
            deleted.append(r.name),
            SimpleNamespace(return_code=0),
        )[1]
        if failure == "stop":
            original_exec = fake.exec_argv
            fake.exec_argv = lambda r, argv, remote_dir=None, execute=original_exec: (
                SimpleNamespace(return_code=1, stdout="", stderr="stop failed")
                if argv[:2] == ("docker", "stop")
                else execute(r, argv, remote_dir)
            )
        nodes[node.id] = _owned_node(
            node,
            VmRequest(lifecycle="multipass", name=node.id, user="ubuntu"),
            fake,
            config=config,
            generator=resource_of("generator", lambda _i: "host", lambda _i, _v: None),
            target_cpus=None,
            pin=lambda *_a: None,
            stop_argv=None,
        )
    built = _build(tmp_path, monkeypatch, events, nodes=nodes)
    if failure is not None and failure != "stop":

        def archive_failed(*_args):
            raise OSError(
                "checksum mismatch" if failure == "checksum" else "archive failed"
            )

        monkeypatch.setattr(platform, "archive_node_dir", archive_failed)
    if failure == "export":

        def export_failed(**_kwargs):
            raise RuntimeError("export failed too")

        monkeypatch.setattr(platform, "export_collector", export_failed)
    if failure == "receipt":
        original_write = Path.write_text

        def write(path, *args, **kwargs):
            if path.name == "recovery.json":
                raise OSError("receipt disk full")
            return original_write(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write)
    if failure is None:
        _run(built, events)
        assert set(deleted) == set(NODES)
        assert not any(n.recovery for n in nodes.values())
    else:
        with pytest.raises(Exception, match=r"failed|mismatch"):
            _run(built, events)
        assert deleted == []
        assert all(n.recovery for n in nodes.values())
    assert len(_of(events, "undeploy")) == len(_deploy_order(tmp_path))
