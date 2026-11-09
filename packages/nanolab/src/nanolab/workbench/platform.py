"""The one-shot platform: every workbench instance of a run as one composite.

Members deploy in order (exporters, Loki, log collectors, Prometheus, the
supervisor) and undeploy in reverse; the collect steps run first at release
and read only the journalled member values, so a later process can run them.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from sonata_engine import Members, Resource, TaskInputs, compose

from nanolab.config.environment import OperatorLogging
from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot.infrastructure import OneShotResources
from nanolab.one_shot.operator_memory import MAX_SWAP_PAGES, MIN_AVAILABLE_BYTES
from nanolab.tasks.one_shot.logging import (
    collection_evidence,
    export_queue_events,
    query_url,
    verify_loki_ingestion,
)
from nanolab.tasks.one_shot.queue_telemetry import queue_timings
from nanolab.tasks.resource_tasks import call
from nanolab.workbench.components import Deployed, component_resource
from nanolab.workbench.install import ensure_installed, workbench_resource
from nanolab.workbench.inventory import HostVars, Instance
from nanolab.workbench.playbook import WorkbenchHost, WorkbenchPlaybook
from nanolab.workbench.supervisor import SupervisorClient
from nanolab.workbench.vm_export import NodeRoles, export_vm_resources

OPERATOR = WorkbenchHost("operator", None)
# Named in plans/one_shot_common, which this package may not import.
CONTROL_PLANE = "nanolab-one-shot-control-plane"
NODE_EXPORTER_PORT = 9100
CADVISOR_PORT = 8089
COLLECTOR_PORT = 2020  # fluent-bit.conf's HTTP_Port
PROMETHEUS_PORT = 9090
SUPERVISOR_PORT = 9095
SCRAPE_SECONDS = 5
PUSH_PATH = "/loki/api/v1/push"
# As LivenessCheck tolerates today: a node behind a site or on Azure may be
# unreachable this long before its checks fault.
REMOTE_UNREACHABLE_SECONDS = 300

type Step = Callable[[Members, TaskInputs], None]


@dataclass(frozen=True)
class OneShotPlatform:
    """The composite and the members later tasks read with `Members.of`."""

    resource: Resource[Members]
    loki: Resource[Deployed]
    prometheus: Resource[Deployed]
    supervisor: Resource[Deployed]
    loadgen_host: WorkbenchHost
    collectors: Mapping[str, Resource[Deployed]]


def _cpus(cpus: list[int] | None) -> str | None:
    return ",".join(map(str, cpus)) if cpus else None


def _cpu_list(cpuset: str | None) -> list[int]:
    found: list[int] = []
    for part in (cpuset or "").split(","):
        if part:
            low, _, high = part.partition("-")
            found += range(int(low), int(high or low) + 1)
    return found


def _inspect(host: WorkbenchHost, name: str) -> dict[str, Any]:
    """`docker inspect` one container: its image and cpuset."""
    argv = ("docker", "inspect", name)
    if host.node is None:
        out = subprocess.run(
            argv, capture_output=True, text=True, check=True, timeout=30
        ).stdout
    else:
        out = host.node.command(argv).stdout
    inspected = json.loads(out)[0]
    return {
        "container": name,
        "image": inspected["Image"],
        "imageReference": inspected["Config"]["Image"],
        "cpuset": inspected["HostConfig"].get("CpusetCpus") or None,
    }


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")


def one_shot_platform(
    topology: OneShotResources,
    settings: OneShotConfig,
    *,
    run_dir: Path,
    nanofaas_root: Path,
    forwards: Mapping[str, Mapping[str, str]],
    mode: str,
    operator_logging: OperatorLogging | None = None,
    log_endpoints: Mapping[str, str] | None = None,
) -> OneShotPlatform:
    """Compose the run's workbench instances on the operator and every node.

    `forwards[node]` holds the local addresses Prometheus scrapes that node's
    exporters at. `mode` is the collectors' NANOFAAS_MODE. A multi-site run
    passes its `operator_logging` and, per node, the push URL its collector
    reaches Loki at (`log_endpoints`); otherwise collectors push to the
    scenario's endpoint.
    """
    logs = settings.observability.logs
    if not logs.backend_owned or not logs.endpoint:
        raise ValueError("the one-shot platform owns its Loki: set backendOwned")
    run_id = run_dir.name
    workbench_dir = run_dir / "workbench"
    vars_dir = workbench_dir / "vars"
    installed = workbench_resource()
    placement = settings.host_cpu_placement
    if operator_logging is not None:
        loki_bind, loki_port = "127.0.0.1", operator_logging.port
        loki_cpus = operator_logging.cpus
        logging_dir = run_dir / "logging" / "backend"
    else:
        loki_bind = logs.backend_bind
        loki_port = urlsplit(logs.endpoint).port or 80
        loki_cpus = placement.logging_cpus if placement else None
        logging_dir = run_dir / "logging"
    loki_url = f"http://{loki_bind}:{loki_port}"
    nodes = {key: WorkbenchHost(key, node) for key, node in topology.nodes.items()}
    hosts = {"operator": OPERATOR, **nodes}

    def member(
        host: WorkbenchHost,
        component: str,
        config: dict[str, Any],
        *,
        url: str | None = None,
        cpuset: str | None = None,
        requires: tuple[Resource[Any], ...] = (),
    ) -> tuple[Resource[Deployed], Instance]:
        instance = Instance.model_validate(
            {
                "name": f"{component}-{host.key}",
                "component": component,
                "cpuset": cpuset,
                "config": config,
            }
        )
        resource = component_resource(
            host,
            instance,
            url=url,
            vars_dir=vars_dir,
            installed=installed,
            requires=requires,
        )
        return resource, instance

    def scraped(key: str, component: str, port: int) -> str:
        return f"127.0.0.1:{port}" if key == "operator" else forwards[key][component]

    exporters = [
        member(
            host,
            component,
            {},
            url="http://" + scraped(key, component, port),
        )
        for key, host in hosts.items()
        for component, port in (
            ("node-exporter", NODE_EXPORTER_PORT),
            ("cadvisor", CADVISOR_PORT),
        )
    ]
    loki, _ = member(
        OPERATOR,
        "loki",
        {
            "port": loki_port,
            "bind": loki_bind,
            "data_dir": str(run_dir / "logging" / "backend" / "backend-data"),
            "config_file": str(nanofaas_root / "deploy/observability/loki.yaml"),
        },
        url=loki_url,
        cpuset=_cpus(loki_cpus),
    )
    config_files = {
        name: nanofaas_root / "deploy/observability" / name
        for name in ("fluent-bit.conf", "parsers.conf")
    }
    collectors: dict[str, tuple[Resource[Deployed], Instance]] = {}
    for key, host in nodes.items():
        push = urlsplit((log_endpoints or {}).get(key, logs.endpoint))
        collectors[key] = member(
            host,
            "log-collector",
            {
                "port": COLLECTOR_PORT,
                "config_files": {k: str(v) for k, v in config_files.items()},
                "loki": {
                    "host": push.hostname,
                    "port": push.port or (443 if push.scheme == "https" else 80),
                    "uri": push.path,
                    "tls": "On" if push.scheme == "https" else "Off",
                },
                # On the node VM, per run; the VM's home is unknown until acquired.
                "storage_dir": f"/var/tmp/nanolab-logs-{run_id}",  # nosec B108
                "cpus": logs.cpu,
                "memory_mib": logs.memory_mib,
                "env": {
                    "NANOFAAS_NODE_ID": key,
                    "NANOFAAS_ENVIRONMENT": logs.environment,
                    "NANOFAAS_CAMPAIGN_ID": logs.campaign_id,
                    "NANOFAAS_RUN_ID": run_id,
                    "NANOFAAS_MODE": mode,
                    "NANOFAAS_NODE_ROLE": topology.nodes[key].config.kind,
                    "NANOFAAS_LOG_BUFFER_SIZE": f"{logs.buffer_mib}M",
                },
            },
            requires=(loki,),
        )
    jobs = {
        job: [
            {
                "target": scraped(key, component, port),
                "labels": {"node": key},
            }
            for key in hosts
        ]
        for job, component, port in (
            ("node", "node-exporter", NODE_EXPORTER_PORT),
            ("cadvisor", "cadvisor", CADVISOR_PORT),
        )
    }
    prometheus_url = f"http://127.0.0.1:{PROMETHEUS_PORT}"
    prometheus, prometheus_instance = member(
        OPERATOR,
        "prometheus",
        {
            "port": PROMETHEUS_PORT,
            "data_dir": str(workbench_dir / "prometheus-data"),
            "scrape_interval": f"{SCRAPE_SECONDS}s",
            "jobs": jobs,
        },
        url=prometheus_url,
    )

    def tolerated(key: str) -> int:
        node = topology.nodes.get(key)
        remote = node is not None and (
            node.site_host is not None or node.request.lifecycle == "azure"
        )
        return REMOTE_UNREACHABLE_SECONDS if remote else 0

    targets: dict[str, dict[str, Any]] = {
        f"up.{job}.{key}": {
            "kind": "scrape_up",
            "params": {"job": job, "labels": {"node": key}},
            "toleratedSeconds": tolerated(key),
        }
        for key in hosts
        for job in jobs
    }
    for key in nodes:
        for role, name in (
            ("control-plane", CONTROL_PLANE),
            ("log-collector", f"wb-log-collector-{key}"),
        ):
            targets[f"running.{role}.{key}"] = {
                "kind": "container_running",
                "params": {"name": name, "node": key},
                "toleratedSeconds": tolerated(key),
            }
    targets["swap.operator"] = {
        "kind": "swap_quiet",
        "params": {
            "node": "operator",
            "maxSwapPages": MAX_SWAP_PAGES,
            "minAvailableBytes": MIN_AVAILABLE_BYTES,
        },
        "toleratedSeconds": 0,
    }
    supervisor, _ = member(
        OPERATOR,
        "supervisor",
        {
            "port": SUPERVISOR_PORT,
            "prometheus_url": prometheus_url,
            "targets": targets,
        },
        url=f"http://127.0.0.1:{SUPERVISOR_PORT}",
        requires=(prometheus,),
    )

    def collect(
        inputs: TaskInputs, host: WorkbenchHost, instance: Instance, value: Deployed
    ) -> None:
        revive = None if host.node is None else host.node.vm.revive
        WorkbenchPlaybook(
            f"Collect {instance.name}",
            host,
            "collect",
            HostVars(instances=(instance,), collect_dir=str(workbench_dir)),
            vars_dir=vars_dir,
            collections=ensure_installed(),
            vm=None if revive is None or value.vm is None else revive(value.vm),
        ).run(inputs)

    def snapshot(values: Members, inputs: TaskInputs) -> None:
        collect(inputs, OPERATOR, prometheus_instance, values.of(prometheus))

    def vm_resources(values: Members, _inputs: TaskInputs) -> None:
        started = values.of(prometheus).deployed_at or time.time()
        observers = ("node-exporter", "cadvisor", "log-collector")
        export_vm_resources(
            prometheus_url,
            run_dir,
            nodes={
                key: NodeRoles(
                    roles={
                        CONTROL_PLANE: "control-plane",
                        f"wb-log-collector-{key}": "collector",
                    },
                    observers=tuple(f"wb-{c}-{key}" for c in observers),
                )
                for key in nodes
            },
            start=datetime.fromtimestamp(started, UTC),
            end=datetime.now(UTC),
            step_seconds=SCRAPE_SECONDS,
        )

    def loki_export(values: Members, _inputs: TaskInputs) -> None:
        facts = _inspect(OPERATOR, "wb-loki-operator")
        placement_file = (
            logging_dir if operator_logging is not None else run_dir
        ) / "logging-host-placement.json"
        _write_json(
            placement_file,
            {"host": "operator", **facts, "cpus": _cpu_list(facts["cpuset"])},
        )
        verify_loki_ingestion(
            loki_url + PUSH_PATH, f"nanolab-logs-{run_id}", logging_dir, settings
        )
        for key, (resource, _instance) in collectors.items():
            directory = run_dir / "logging" / key
            directory.mkdir(parents=True, exist_ok=True)
            query = (
                '{job="nanofaas",node='
                + json.dumps(key)
                + "} | json | runId="
                + json.dumps(run_id)
                + ' | logger="it.unimib.datai.nanofaas.execution.QueueObservation"'
            )
            started = values.of(resource).deployed_at or time.time()
            with httpx.Client(trust_env=False) as http:
                export_queue_events(
                    http,
                    query_url(loki_url + PUSH_PATH),
                    query,
                    int(started * 1e9),
                    time.time_ns(),
                    logs.export_limit,
                    directory / "queue-events.jsonl",
                )
            events = directory / "queue-events.jsonl"
            rows = (
                [json.loads(line) for line in events.read_text().splitlines()]
                if events.exists()
                else []
            )
            (directory / "queue-timings.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in queue_timings(rows))
            )

    def collector_step(key: str) -> Step:
        resource, instance = collectors[key]
        host = nodes[key]

        def run(values: Members, inputs: TaskInputs) -> None:
            collect(inputs, host, instance, values.of(resource))
            metrics = (workbench_dir / instance.name / "metrics.txt").read_text()
            directory = run_dir / "logging" / key
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "collector-metrics.txt").write_text(metrics)
            evidence = collection_evidence(metrics)
            issues = (
                []
                if evidence["status"] == "drained"
                else ["collection delivery is " + evidence["status"]]
            )
            _write_json(
                directory / "status.json",
                {
                    "status": "partial" if issues else "complete",
                    "nodeId": key,
                    "issues": issues,
                    "collection": evidence,
                    "identity": {
                        **_inspect(host, f"wb-{instance.name}"),
                        "configSha256": {
                            name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for name, path in config_files.items()
                        },
                        "cpu": logs.cpu,
                        "memoryMiB": logs.memory_mib,
                        "bufferMiB": logs.buffer_mib,
                    },
                },
            )

        return run

    def supervisor_status(values: Members, _inputs: TaskInputs) -> None:
        url = values.of(supervisor).url or f"http://127.0.0.1:{SUPERVISOR_PORT}"
        _write_json(
            workbench_dir / "supervisor-status.json",
            SupervisorClient(url, timeout_seconds=10).status(),
        )

    steps: list[tuple[str, Step]] = [
        ("Snapshot the workbench Prometheus", snapshot),
        ("Export vm-resources", vm_resources),
        ("Export and verify Loki", loki_export),
        *((f"Collect the {key} log collector", collector_step(key)) for key in nodes),
        ("Record the supervisor status", supervisor_status),
    ]

    def step(fn: Step) -> Callable[[TaskInputs], None]:
        # Collect steps get the members' values as their upstream.
        return lambda inputs: fn(cast(Members, inputs.upstream()), inputs)

    resource = compose(
        "Acquire the one-shot workbench platform",
        (
            *(resource for resource, _ in exporters),
            loki,
            *(resource for resource, _ in collectors.values()),
            prometheus,
            supervisor,
        ),
        collect=tuple(call(title, step(fn)) for title, fn in steps),
    )
    return OneShotPlatform(
        resource,
        loki,
        prometheus,
        supervisor,
        OPERATOR,
        {key: resource for key, (resource, _) in collectors.items()},
    )
