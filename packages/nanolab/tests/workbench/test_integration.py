"""Opt-in Linux smoke against installed wheels and collection.

Run with NANOLAB_WORKBENCH_INTEGRATION=1 and NANOLAB_WORKSPACE pointing to
an isolated workspace with the pinned collection already installed.
"""

import json
import os
import re
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

import pytest
from sonata_engine import JournalConfig, TaskInputs, Workflow, compose
from sonata_engine.journal import Journal

from nanolab.one_shot.vm_resources import summarize_resources
from nanolab.tasks.one_shot.experiment import generator_rows
from nanolab.tasks.resource_tasks import acquire_now, call, release_now, resource_of
from nanolab.workbench.components import component_resource
from nanolab.workbench.install import ensure_installed
from nanolab.workbench.inventory import HostVars, Instance
from nanolab.workbench.loadgen import loadgen_resource
from nanolab.workbench.platform import OPERATOR
from nanolab.workbench.playbook import WorkbenchPlaybook
from nanolab.workbench.vm_export import NodeRoles, export_vm_resources

pytestmark = pytest.mark.skipif(
    os.environ.get("NANOLAB_WORKBENCH_INTEGRATION") != "1",
    reason="opt-in installed workbench integration",
)


@pytest.fixture(autouse=True)
def isolated_operator_workspace():
    """Use the explicitly selected integration workspace and its collection."""
    if not os.environ.get("NANOLAB_WORKSPACE"):
        pytest.fail("integration tests require an isolated NANOLAB_WORKSPACE")


def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=120
    )


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if result := check():
            return result
        time.sleep(1)
    raise AssertionError("condition not reached before deadline")


def test_real_composite_journal_failure_removes_container(tmp_path, monkeypatch):
    name = "nl-review-journal-" + uuid4().hex[:8]
    original = Journal.record_passed

    def fail(self, task_id, attempt, evidence=()):
        if task_id.endswith("/acquire-acquire-container"):
            raise OSError("injected terminal journal failure")
        return original(self, task_id, attempt, evidence)

    monkeypatch.setattr(Journal, "record_passed", fail)
    resource = resource_of(
        "Acquire container",
        lambda _: (
            docker("run", "-d", "--name", name, "busybox:1.36", "sleep", "120").stdout
        ),
        lambda *_: docker("rm", "-f", name),
    )
    workflow = Workflow("real-journal-failure")
    workflow.add(
        call("Consumer", lambda _: pytest.fail("consumer must not run")),
        requires=(compose("Acquire platform", (resource,)),),
    )
    try:
        with pytest.raises(OSError, match="terminal journal"):
            workflow.run(journal=JournalConfig(tmp_path / "journal.jsonl"))
        assert docker("inspect", name, check=False).returncode != 0
    finally:
        docker("rm", "-f", name, check=False)


def test_installed_components_load_cpu_export_and_fault(tmp_path):
    root = Path(os.environ.get("NANOLAB_INTEGRATION_EVIDENCE", str(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)
    uid = uuid4().hex[:8]
    ports = {name: port() for name in ("ne", "cad", "pr", "sv")}
    names = {name: f"review-{name}-{uid}" for name in ports}
    cpu = min(os.sched_getaffinity(0))
    instances = [
        Instance(
            name=names["ne"],
            component="node-exporter",
            cpuset=str(cpu),
            config={"port": ports["ne"], "cpufreq": False},
        ),
        Instance(
            name=names["cad"],
            component="cadvisor",
            cpuset=str(cpu),
            config={"port": ports["cad"], "housekeeping_interval": "1s"},
        ),
        Instance(
            name=names["pr"],
            component="prometheus",
            cpuset=str(cpu),
            config={
                "port": ports["pr"],
                "data_dir": str(root / "tsdb"),
                "scrape_interval": "1s",
                "jobs": {
                    job: [
                        {
                            "target": f"127.0.0.1:{ports[key]}",
                            "labels": {"node": "test"},
                        }
                    ]
                    for job, key in (("node", "ne"), ("cadvisor", "cad"))
                },
            },
        ),
        Instance(
            name=names["sv"],
            component="supervisor",
            cpuset=str(cpu),
            config={
                "port": ports["sv"],
                "prometheus_url": f"http://127.0.0.1:{ports['pr']}",
                "interval": 1,
                "targets": {
                    "cadvisor": {
                        "kind": "scrape_up",
                        "params": {"job": "cadvisor"},
                        "graceSeconds": 10,
                        "toleratedSeconds": 0,
                    }
                },
            },
        ),
    ]
    resources = [
        component_resource(
            OPERATOR, instance, url=None, vars_dir=root / "vars", installed=None
        )
        for instance in instances
    ]
    url = f"http://127.0.0.1:{ports['pr']}"

    def query(expression):
        with urlopen(
            url + "/api/v1/query?" + urlencode({"query": expression}), timeout=5
        ) as response:
            return json.load(response)["data"]["result"]

    class Echo(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"status":"success","output":7}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def observe(_inputs):
        wait_for(
            lambda: query(f'container_memory_usage_bytes{{name="wb-{names["ne"]}"}}')
        )
        schedule = [
            {
                "originalId": f"row-{i}",
                "phase": "campaign",
                "input": {},
                "url": f"http://127.0.0.1:{server.server_port}/",
                "scheduledAt": time.time() + 30 + i,
            }
            for i in range(2)
        ]
        (root / "schedule.json").write_text(json.dumps(schedule))
        generator = loadgen_resource(
            OPERATOR, run_dir=root, vus=1, duration=45, cpuset=str(cpu)
        )
        load = acquire_now(generator, TaskInputs.empty())
        try:
            masks = {}
            for name in [*("wb-" + i.name for i in instances), load.container]:
                inspected = json.loads(docker("inspect", name).stdout)[0]
                assert inspected["HostConfig"]["CpusetCpus"] == str(cpu)
                pid = inspected["State"]["Pid"]
                assert pid > 0, name
                masks[name] = {
                    str(t.name): sorted(os.sched_getaffinity(int(t.name)))
                    for t in Path(f"/proc/{pid}/task").iterdir()
                }
                assert all(mask == [cpu] for mask in masks[name].values())
            (root / "cpu-affinity.json").write_text(json.dumps(masks, indent=2))
            wait_for(lambda: load.poll() is not None)
            assert load.returncode == 0
            results = [
                r
                for r in generator_rows(root / "generator.jsonl")
                if r["event"] == "result"
            ]
            assert len(results) == 2 and all(r["statusCode"] == 200 for r in results)
        finally:
            release_now(generator, TaskInputs.empty(), load)

        def export(label):
            anchor = datetime.fromtimestamp(int(time.time()) - 8, UTC)
            directory = root / label
            export_vm_resources(
                url,
                directory,
                nodes={"test": NodeRoles({f"wb-{names['ne']}": "control-plane"})},
                start=anchor - timedelta(seconds=2),
                end=anchor + timedelta(seconds=6),
                step_seconds=1,
            )
            coverage = summarize_resources(
                directory,
                anchor=anchor,
                warmup=0,
                period=4,
                epochs=1,
                nodes=["test"],
                mode="baseline",
                functions=[],
                run_id=uid,
            )
            (directory / "coverage.json").write_text(json.dumps(coverage, indent=2))
            return coverage

        assert export("normal")["complete"]
        WorkbenchPlaybook(
            "Snapshot",
            OPERATOR,
            "collect",
            HostVars(instances=(instances[2],), collect_dir=str(root / "workbench")),
            vars_dir=root / "vars",
            collections=ensure_installed(),
            vm=None,
        ).run(TaskInputs.empty())
        assert (
            root
            / "workbench"
            / "localhost"
            / names["pr"]
            / "prometheus-snapshot.tar.gz"
        ).is_file()
        cad = "wb-" + names["cad"]
        inspected = json.loads(docker("inspect", cad).stdout)[0]
        docker("rm", "-f", cad)
        docker(
            "run",
            "-d",
            "--name",
            cad,
            "--label",
            f"nanofaas.workbench/instance={names['cad']}",
            "--cpuset-cpus",
            str(cpu),
            "-p",
            f"127.0.0.1:{ports['cad']}:8080",
            inspected["Config"]["Image"],
            "--docker_only=true",
            "--docker=unix:///missing.sock",
        )
        wait_for(lambda: query('up{job="cadvisor"}')[0]["value"][1] == "1")
        wait_for(lambda: not query('container_memory_usage_bytes{name!=""}'))
        time.sleep(10)
        assert not export("no-discovery")["complete"]
        docker("stop", cad)

        def fault():
            with urlopen(
                f"http://127.0.0.1:{ports['sv']}/status", timeout=5
            ) as response:
                report = json.load(response)
            if report["state"] == "fault":
                (root / "supervisor-fault.json").write_text(
                    json.dumps(report, indent=2)
                )
                return True
            return False

        wait_for(fault)

    workflow = Workflow("installed-workbench-smoke")
    workflow.add(
        call("Observe real workload", observe),
        requires=(compose("Acquire components", tuple(resources)),),
    )
    try:
        workflow.run(journal=JournalConfig(root / "workflow.jsonl"))
    finally:
        server.shutdown()
        server.server_close()
    assert all(
        docker("inspect", "wb-" + i.name, check=False).returncode != 0
        for i in instances
    )


def test_real_cpu_mismatch_refuses_acquire_and_removes_container(tmp_path, monkeypatch):
    cpus = sorted(os.sched_getaffinity(0))
    if len(cpus) < 2:
        pytest.skip("CPU mismatch test needs two CPUs")
    name = "review-cpu-" + uuid4().hex[:8]
    instance = Instance(
        name=name,
        component="node-exporter",
        cpuset=str(cpus[0]),
        config={"port": port(), "cpufreq": False},
    )
    original = WorkbenchPlaybook.run

    def change_actual_placement(self, inputs):
        outcome = original(self, inputs)
        if self.action == "deploy":
            docker("update", "--cpuset-cpus", str(cpus[1]), "wb-" + name)
            observed = json.loads(docker("inspect", "wb-" + name).stdout)[0]
            assert observed["HostConfig"]["CpusetCpus"] == str(cpus[1])
            root = Path(os.environ.get("NANOLAB_INTEGRATION_EVIDENCE", str(tmp_path)))
            root.mkdir(parents=True, exist_ok=True)
            (root / "cpu-mismatch.json").write_text(
                json.dumps(
                    {"requested": cpus[0], "observed": cpus[1], "container": name}
                )
            )
        return outcome

    monkeypatch.setattr(WorkbenchPlaybook, "run", change_actual_placement)
    resource = component_resource(
        OPERATOR, instance, url=None, vars_dir=tmp_path, installed=None
    )
    try:
        with pytest.raises(ValueError, match="CPU placement"):
            acquire_now(resource, TaskInputs.empty())
        assert docker("inspect", "wb-" + name, check=False).returncode != 0
    finally:
        docker("rm", "-f", "wb-" + name, check=False)


def test_receipts_from_production_journal_config_survive_container_removal(tmp_path):
    """Exercise the real NanoFaaS parser and Nanolab correlation after deletion."""
    from types import SimpleNamespace

    from nanolab.tasks.one_shot.receipts import collect_log_receipts

    source = Path(os.environ["NANOLAB_JOURNAL_CONFIG"]).parent
    collections = ensure_installed()
    token = "receipts-" + uuid4().hex[:10]
    loki_port, collector_port = port(), port()
    config = (source / "fluent-bit-journal.conf").read_text()
    config = re.sub(r"(HTTP_Port\s+)2020", rf"\g<1>{collector_port}", config)
    config_file = tmp_path / "fluent-bit.conf"
    config_file.write_text(config)
    loki = Instance(
        name=token + "-loki",
        component="loki",
        config={"port": loki_port, "data_dir": str(tmp_path / "loki")},
    )
    collector = Instance(
        name=token + "-collector",
        component="log-collector",
        config={
            "port": collector_port,
            "journal": True,
            "storage_dir": str(tmp_path / "buffer"),
            "config_files": {
                "fluent-bit.conf": str(config_file),
                "parsers.conf": str(source / "parsers.conf"),
            },
            "loki": {"host": "127.0.0.1", "port": loki_port},
            "env": {
                "NANOFAAS_NODE_ID": "edge",
                "NANOFAAS_ENVIRONMENT": token,
                "NANOFAAS_CAMPAIGN_ID": token,
                "NANOFAAS_RUN_ID": tmp_path.name,
                "NANOFAAS_MODE": "test",
                "NANOFAAS_NODE_ROLE": "edge",
                "NANOFAAS_LOG_BUFFER_SIZE": "16M",
            },
        },
    )

    def play(action, instances):
        WorkbenchPlaybook(
            "Journal receipts " + action,
            OPERATOR,
            action,
            HostVars(instances=tuple(instances)),
            vars_dir=tmp_path / "vars",
            collections=collections,
            vm=None,
        ).run(TaskInputs.empty())

    url = f"http://127.0.0.1:{loki_port}"
    try:
        play("deploy", [loki])
        started = time.time()
        rows, events = [], []
        for i in range(50):
            identity = f"{token}-{i}"
            rows.append(
                {
                    "event": "result",
                    "phase": "campaign",
                    "originalId": identity,
                    "executionId": identity,
                    "terminalExecutionId": identity,
                    "origin": "edge",
                    "executionNode": "edge",
                    "function": "work",
                    "statusCode": 200,
                    "responseStatus": "success",
                    "scheduledAt": started,
                }
            )
            events.append(
                {
                    "schemaVersion": 1,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "service": "function-runtime",
                    "logger": "nanofaas::receipt",
                    "executionId": identity,
                    "attributes": {
                        "event": "runtime.execution.released",
                        "receiptSchemaVersion": 1,
                        "incarnation": token,
                        "dispatchAttempt": "1",
                        "state": "RELEASED",
                        "occupancySeconds": 0.25,
                        "responseStatus": "success",
                        "handlerStarted": True,
                    },
                }
            )
        produced = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                token,
                "--log-driver=journald",
                "-i",
                "busybox:1.36",
                "cat",
            ],
            input="".join(json.dumps(e) + "\n" for e in events),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert produced.returncode == 0, produced.stderr
        assert docker("inspect", token, check=False).returncode != 0
        play("deploy", [collector])

        def ready():
            try:
                with urlopen(url + "/ready", timeout=2) as response:
                    return response.status == 200
            except OSError:
                return False

        wait_for(ready)
        run = SimpleNamespace(
            run_dir=tmp_path,
            topology=SimpleNamespace(
                platform=SimpleNamespace(receipt_endpoint=url + "/loki/api/v1/push"),
                endpoints={"edge": "http://edge"},
            ),
            settings=SimpleNamespace(
                observability=SimpleNamespace(logs=SimpleNamespace(export_limit=100))
            ),
        )
        collect_log_receipts(
            run,
            SimpleNamespace(resource=lambda value: value),
            rows,
            deadline=time.monotonic() + 30,
        )
        proofs = [
            json.loads(line)
            for line in (tmp_path / "physical.jsonl").read_text().splitlines()
        ]
        assert {p["executionId"] for p in proofs} == {r["executionId"] for r in rows}
        assert len(proofs) == 50
    finally:
        play("undeploy", [collector, loki])
