"""The one-shot platform: every workbench instance of a run as one composite.

Members deploy in order (the operator preflight, the loadgen image pull,
exporters, Loki, log collectors, Prometheus, the supervisor) and undeploy in
reverse; the collect steps run first at release and read only the journalled
member values, so a later process can run them.
Without logs the platform is the exporters, Prometheus and the supervisor.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import yaml
from sonata_engine import Members, Resource, TaskInputs, compose

from nanolab.config.environment import OperatorLogging
from nanolab.config.one_shot import OneShotConfig, OneShotLogs, operational_limit
from nanolab.one_shot.addresses import CADVISOR_PORT, NODE_EXPORTER_PORT
from nanolab.one_shot.infrastructure import NodeResource, OneShotResources
from nanolab.one_shot.operator_memory import MAX_SWAP_PAGES
from nanolab.one_shot.vm_resources import SCRAPE_SECONDS
from nanolab.tasks.one_shot.logging import (
    LogSession,
    archive_node_dir,
    collection_evidence,
    export_collector,
    retain_collector_for_recovery,
    verify_loki_ingestion,
)
from nanolab.tasks.resource_tasks import call, resource_of
from nanolab.workbench.components import Deployed, component_resource
from nanolab.workbench.install import (
    check_operator,
    ensure_installed,
    workbench_resource,
)
from nanolab.workbench.inventory import HostVars, Instance
from nanolab.workbench.loadgen import LoadgenRun, loadgen_resource
from nanolab.workbench.playbook import WorkbenchHost, WorkbenchPlaybook
from nanolab.workbench.supervisor import SupervisorClient
from nanolab.workbench.vm_export import NodeRoles, export_vm_resources
from nanolab.workspace.paths import operator_workspace_root

OPERATOR = WorkbenchHost("operator", None)
# Named in plans/one_shot_common, which this package may not import.
CONTROL_PLANE = "nanolab-one-shot-control-plane"
COLLECTOR_PORT = 2020  # fluent-bit.conf's HTTP_Port
PROMETHEUS_PORT = 9090
SUPERVISOR_PORT = 8099
# cAdvisor housekeeping plus scrape (5 s) jitter, with room to spare.
STALE_SECONDS = 30
# cAdvisor's 5 s default, jittered and backed off, aliases a 5 s irate.
HOUSEKEEPING = "1s"
# Swap traffic may last this long before a run faults: memory headroom is
# checked before the run, while the operator hosts Prometheus, Loki and k6.
SWAP_TOLERATED_SECONDS = 30
PUSH_PATH = "/loki/api/v1/push"
# As the in-process liveness checks tolerated: a node behind a site or on Azure
# may be unreachable this long before its checks fault.
REMOTE_UNREACHABLE_SECONDS = 300
# Today's bounded drain wait: up to five metric reads two seconds apart.
DRAIN_POLLS = 5
DRAIN_POLL_SECONDS = 2
LOKI_STORE = "Own the Loki store"
PROMETHEUS_STORE = "Own the Prometheus store"
PREFLIGHT = "Check the operator prerequisites"

type Step = Callable[[Members, TaskInputs], None]


@dataclass(frozen=True)
class OneShotPlatform:
    """The composite and the members later tasks read with `Members.of`.

    `loki` is None and `collectors` empty when the scenario has no logs.
    `collector_identity(node)` measures a running collector, as the
    environment fingerprint records it. `targets` are every supervisor target
    of the run; the supervisor starts with all but the control planes', which
    are registered once those are up. `export_vm_resources(into, start, end)`
    writes a window's vm-resources rows under `into`. `loadgen(...)` is
    `loadgen_resource` on the nanolab host, for tasks that may not import the
    workbench. `record_supervisor_status(values)` writes the supervisor's
    status into the run, from the composite's values; a supervised run calls
    it before removing its targets.
    """

    resource: Resource[Members]
    loki: Resource[Deployed] | None
    prometheus: Resource[Deployed]
    supervisor: Resource[Deployed]
    collectors: Mapping[str, Resource[Deployed]]
    collector_identity: Callable[[str], dict[str, Any]]
    targets: Mapping[str, dict[str, Any]]
    export_vm_resources: Callable[[Path, datetime, datetime], None]
    loadgen: Callable[..., Resource[LoadgenRun]]
    record_supervisor_status: Callable[[Members], None]


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
    """`docker inspect` one container: image, cpuset and CPU/memory limits."""
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
        "nanoCpus": inspected["HostConfig"].get("NanoCpus"),
        "memoryBytes": inspected["HostConfig"].get("Memory"),
    }


def _loki_log(name: str) -> str:
    done = subprocess.run(
        ("docker", "logs", name),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return done.stdout + done.stderr


def _operator_container_exists(name: str) -> bool:
    """Whether a container named `name` is still on the operator."""
    return (
        subprocess.run(
            ("docker", "container", "inspect", name),
            capture_output=True,
            check=False,
            timeout=30,
        ).returncode
        == 0
    )


def _as_root(path: Path, *argv: str) -> None:
    """Run `argv` as root on `path`, mounted at /data.

    For a store a component's own uid wrote (Loki 10001, Prometheus nobody).
    """
    defaults = (
        ensure_installed()
        / "ansible_collections/nanofaas/workbench/roles/common/defaults/main.yml"
    )
    image = yaml.safe_load(defaults.read_text())["common_probe_image"]
    subprocess.run(
        (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "-v",
            f"{path}:/data",
            image,
            *argv,
        ),
        capture_output=True,
        check=True,
        timeout=300,
    )


def _pull_loadgen(collections: Path) -> None:
    """Pull the pinned k6 image, so a loadgen deploy never waits on a pull."""
    defaults = (
        collections
        / "ansible_collections/nanofaas/workbench/roles/loadgen/defaults/main.yml"
    )
    image = yaml.safe_load(defaults.read_text())["loadgen_image"]
    subprocess.run(
        ("docker", "pull", image), capture_output=True, check=True, timeout=600
    )


def _loki_config(source: Path, into: Path) -> Path:
    """Write nanoFaaS' Loki config with today's WAL flags as YAML keys.

    Equivalent to `-ingester.wal-enabled=true` and
    `-ingester.wal-disk-full-threshold=0` (verified with Loki 3.7.8's
    `-print-config-stderr`).
    """
    config = yaml.safe_load(source.read_text())
    config.setdefault("ingester", {}).setdefault("wal", {}).update(
        enabled=True, disk_full_threshold=0
    )
    into.parent.mkdir(parents=True, exist_ok=True)
    into.write_text(yaml.safe_dump(config, sort_keys=False))
    return into


def _loki_target(push_url: str) -> dict[str, Any]:
    push = urlsplit(push_url)
    return {
        "host": push.hostname,
        "port": push.port or (443 if push.scheme == "https" else 80),
        "uri": push.path,
        "tls": "On" if push.scheme == "https" else "Off",
    }


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")


def _remote(node: NodeResource | None) -> bool:
    return node is not None and (
        node.site_host is not None or node.request.lifecycle == "azure"
    )


def one_shot_platform(
    topology: OneShotResources,
    settings: OneShotConfig,
    *,
    run_dir: Path,
    nanofaas_root: Path,
    forwards: Mapping[str, Mapping[str, str]],
    mode: str,
    operator_logging: OperatorLogging | None = None,
    log_endpoints: Mapping[str, Resource[str]] | None = None,
    bootstraps: Mapping[str, Resource[Any]] | None = None,
) -> OneShotPlatform:
    """Compose the run's workbench instances on the operator and every node.

    Node exporters listen on every VM address. A node in `forwards` is
    scraped at the addresses those publish it on (a site's forwarded
    ports); any other node at its VM's address. `mode` is the collectors'
    NANOFAAS_MODE. A multi-site run passes its `operator_logging` and, per
    node, the route whose value is the push URL its collector reaches Loki at
    (`log_endpoints`); otherwise collectors push to the scenario's endpoint.
    A node's members also require its entry in `bootstraps` (Docker there).
    """
    logs = settings.observability.logs
    if logs.enabled and not logs.backend_owned:
        raise ValueError(
            "the one-shot platform runs its own Loki: an external logging "
            "endpoint is not supported, set backendOwned"
        )
    if logs.enabled and logs.image != OneShotLogs.model_fields["image"].default:
        raise ValueError(
            "logs.image is not configurable: the workbench pins the "
            "log-collector image by digest"
        )
    bootstraps = bootstraps or {}
    run_id = run_dir.name
    # Operator instances are run-unique: runs share the host, VMs are run-owned.
    operator_id = hashlib.sha256(str(run_dir.resolve()).encode()).hexdigest()[:8]
    workbench_dir = run_dir / "workbench"
    vars_dir = workbench_dir / "vars"
    installed = workbench_resource()
    nodes = {key: WorkbenchHost(key, node) for key, node in topology.nodes.items()}
    hosts = {"operator": OPERATOR, **nodes}
    direct = {k: n for k, n in topology.nodes.items() if k not in forwards}

    placement = settings.host_cpu_placement
    operator_cpus = _cpus(placement.operator_cpus) if placement else None

    def member(
        host: WorkbenchHost,
        component: str,
        config: dict[str, Any],
        *,
        url: str | None = None,
        cpuset: str | None = None,
        requires: tuple[Resource[Any], ...] = (),
        config_from_inputs: Callable[[TaskInputs], dict[str, Any]] | None = None,
    ) -> tuple[Resource[Deployed], Instance]:
        if host.node is not None and host.key in bootstraps:
            requires = (bootstraps[host.key], *requires)
        if host.node is None and component != "loki" and cpuset is None:
            cpuset = operator_cpus
        instance = Instance.model_validate(
            {
                "name": f"{component}-{host.key}"
                + ("" if host.node is not None else f"-{operator_id}"),
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
            config_from_inputs=config_from_inputs,
        )
        return resource, instance

    exporter_ports = {"node-exporter": NODE_EXPORTER_PORT, "cadvisor": CADVISOR_PORT}
    exporters = [
        member(
            host,
            component,
            ({} if host.node is None else {"bind": "0.0.0.0"})  # nosec B104 - docstring
            | (
                {"housekeeping_interval": HOUSEKEEPING}
                if component == "cadvisor"
                else {}
            ),
            url=None
            if key in direct
            else "http://"
            + (f"127.0.0.1:{port}" if host.node is None else forwards[key][component]),
        )
        for key, host in hosts.items()
        for component, port in exporter_ports.items()
    ]
    pull = resource_of(
        "Pull the loadgen image",
        lambda inputs: _pull_loadgen(inputs.resource(installed)),
        lambda _inputs, _value: None,
        requires=(installed,),
    )
    members: list[Resource[Any]] = [pull, *(resource for resource, _ in exporters)]
    operator_ports = [
        ("127.0.0.1", port)
        for port in (PROMETHEUS_PORT, SUPERVISOR_PORT, *exporter_ports.values())
    ]

    # Logging: Loki on the operator, a collector on every node.
    loki: Resource[Deployed] | None = None
    collectors: dict[str, tuple[Resource[Deployed], Instance]] = {}
    if operator_logging is not None:
        logging_dir = run_dir / "logging" / "backend"
    else:
        logging_dir = run_dir / "logging"
    loki_url = ""
    loki_container = ""
    config_files = {
        name: nanofaas_root / "deploy/observability" / name
        for name in ("fluent-bit.conf", "parsers.conf")
    }
    storage_dir = f"/var/tmp/nanolab-logs-{run_id}"  # nosec B108 - on the node VM
    if logs.enabled:
        if operator_logging is not None:
            loki_bind, loki_port = "127.0.0.1", operator_logging.port
            loki_cpus = operator_logging.cpus
        else:
            placement = settings.host_cpu_placement
            loki_bind = logs.backend_bind
            loki_port = urlsplit(logs.endpoint or "").port or 80
            loki_cpus = placement.logging_cpus if placement else None
        loki_url = f"http://{loki_bind}:{loki_port}"
        operator_ports.append((loki_bind, loki_port))
        data = logging_dir / "backend-data"

        def hand_back(_inputs: TaskInputs, _value: None) -> None:
            if not data.is_dir():
                return
            _as_root(data, "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/data")
            hashes = {}
            for path in sorted(data.rglob("*")):
                if path.is_file():
                    with path.open("rb") as stream:
                        hashes[str(path.relative_to(data))] = hashlib.file_digest(
                            stream, "sha256"
                        ).hexdigest()
            _write_json(logging_dir / "backend-data-sha256.json", hashes)

        # Released after Loki, so the store it hashes is final.
        store = resource_of(LOKI_STORE, lambda _inputs: None, hand_back)
        loki, loki_instance = member(
            OPERATOR,
            "loki",
            {
                "port": loki_port,
                "bind": loki_bind,
                "data_dir": str(data),
                "config_file": str(
                    _loki_config(
                        nanofaas_root / "deploy/observability/loki.yaml",
                        logging_dir / "loki.yaml",
                    )
                ),
            },
            url=loki_url,
            cpuset=_cpus(loki_cpus),
            requires=(store,),
        )
        members += [store, loki]
        loki_container = f"wb-{loki_instance.name}"
        for key, host in nodes.items():
            route = (log_endpoints or {}).get(key)
            collectors[key] = member(
                host,
                "log-collector",
                {
                    "port": COLLECTOR_PORT,
                    "config_files": {k: str(v) for k, v in config_files.items()},
                    **({} if route else {"loki": _loki_target(logs.endpoint or "")}),
                    "storage_dir": storage_dir,
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
                requires=(loki,) if route is None else (loki, route),
                # A route's push URL exists only once it is open.
                config_from_inputs=None
                if route is None
                else lambda inputs, route=route: {
                    "loki": _loki_target(inputs.resource(route))
                },
            )
            members.append(collectors[key][0])

    jobs = {"node": "node-exporter", "cadvisor": "cadvisor"}

    def scrape_jobs(inputs: TaskInputs) -> dict[str, Any]:
        # A direct node's address exists only once its VM is acquired.
        def target(key: str, component: str) -> str:
            port = exporter_ports[component]
            if key == "operator":
                return f"127.0.0.1:{port}"
            if key in direct:
                return f"{inputs.resource(direct[key].vm).host}:{port}"
            return forwards[key][component]

        return {
            "jobs": {
                job: [
                    {"target": target(key, component), "labels": {"node": key}}
                    for key in hosts
                ]
                for job, component in jobs.items()
            }
        }

    prometheus_url = f"http://127.0.0.1:{PROMETHEUS_PORT}"
    # Only the snapshot belongs in the run directory.
    tsdb = operator_workspace_root() / ".workbench" / operator_id / "prometheus"

    def drop_tsdb(_inputs: TaskInputs, _value: None) -> None:
        # Released after Prometheus: its TSDB goes once the snapshot is kept.
        # A failed undeploy leaves Prometheus up on its TSDB: keep it then.
        kept = (
            workbench_dir
            / "localhost"
            / prometheus_instance.name
            / "prometheus-snapshot.tar.gz"
        )
        if (
            kept.is_file()
            and tsdb.is_dir()
            and not _operator_container_exists(f"wb-{prometheus_instance.name}")
        ):
            _as_root(tsdb, "find", "/data", "-mindepth", "1", "-delete")
            shutil.rmtree(tsdb.parent)

    tsdb_store = resource_of(PROMETHEUS_STORE, lambda _inputs: None, drop_tsdb)
    prometheus, prometheus_instance = member(
        OPERATOR,
        "prometheus",
        {
            "port": PROMETHEUS_PORT,
            "data_dir": str(tsdb),
            "scrape_interval": f"{SCRAPE_SECONDS}s",
        },
        url=prometheus_url,
        requires=(tsdb_store, *(node.vm for node in direct.values())),
        config_from_inputs=scrape_jobs,
    )

    def tolerated(key: str) -> int:
        return REMOTE_UNREACHABLE_SECONDS if _remote(topology.nodes.get(key)) else 0

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
        watched = {"control-plane": CONTROL_PLANE}
        if logs.enabled:
            watched["log-collector"] = f"wb-log-collector-{key}"
        for role, name in watched.items():
            targets[f"running.{role}.{key}"] = {
                "kind": "container_running",
                "params": {"name": name, "node": key, "staleSeconds": STALE_SECONDS},
                "toleratedSeconds": tolerated(key),
            }
    targets["swap.operator"] = {
        "kind": "swap_quiet",
        "params": {
            "node": "operator",
            "maxSwapPages": MAX_SWAP_PAGES,
            "minAvailableBytes": 0,
        },
        "toleratedSeconds": SWAP_TOLERATED_SECONDS,
    }
    if logs.enabled:
        targets["running.loki.operator"] = {
            "kind": "container_running",
            "params": {
                "name": loki_container,
                "node": "operator",
                "staleSeconds": STALE_SECONDS,
            },
            "toleratedSeconds": 0,
        }
        targets["ready.loki.operator"] = {
            "kind": "http_ok",
            "params": {"url": f"{loki_url}/ready"},
            "toleratedSeconds": 0,
        }
    supervisor, _ = member(
        OPERATOR,
        "supervisor",
        {
            "port": SUPERVISOR_PORT,
            "prometheus_url": prometheus_url,
            "targets": {
                name: target
                for name, target in targets.items()
                if not name.startswith("running.control-plane.")
            },
        },
        url=f"http://127.0.0.1:{SUPERVISOR_PORT}",
        requires=(prometheus,),
    )
    members += [tsdb_store, prometheus, supervisor]
    preflight = resource_of(
        PREFLIGHT, lambda _inputs: check_operator(operator_ports), lambda _i, _v: None
    )
    members.insert(0, preflight)

    def collect(
        inputs: TaskInputs, host: WorkbenchHost, instance: Instance, value: Deployed
    ) -> None:
        revive = None if host.node is None else host.node.vm.revive
        resolved = instance.model_copy(
            update={"config": {**instance.config, **(value.config or {})}}
        )
        WorkbenchPlaybook(
            f"Collect {instance.name}",
            host,
            "collect",
            HostVars(instances=(resolved,), collect_dir=str(workbench_dir)),
            vars_dir=vars_dir,
            collections=ensure_installed(),
            vm=None if revive is None or value.vm is None else revive(value.vm),
        ).run(inputs)

    def snapshot(values: Members, inputs: TaskInputs) -> None:
        collect(inputs, OPERATOR, prometheus_instance, values.of(prometheus))

    observers = ("node-exporter", "cadvisor") + (
        ("log-collector",) if logs.enabled else ()
    )
    node_roles = {
        key: NodeRoles(
            roles={CONTROL_PLANE: "control-plane"}
            | ({f"wb-log-collector-{key}": "collector"} if logs.enabled else {}),
            observers=tuple(f"wb-{c}-{key}" for c in observers),
        )
        for key in nodes
    }

    def export(into: Path, start: datetime, end: datetime) -> None:
        export_vm_resources(
            prometheus_url,
            into,
            nodes=node_roles,
            start=start,
            end=end,
            step_seconds=SCRAPE_SECONDS,
        )

    # Exports query the operator's Loki, as today's multi-site copy of `logs`.
    export_logs = logs.model_copy(update={"endpoint": loki_url + PUSH_PATH})

    def collector_budget() -> dict[str, Any]:
        return {
            "configSha256": {
                file: hashlib.sha256(path.read_bytes()).hexdigest()
                for file, path in config_files.items()
            },
            "cpu": logs.cpu,
            "memoryMiB": logs.memory_mib,
            "bufferMiB": logs.buffer_mib,
        }

    def collector_identity(key: str) -> dict[str, Any]:
        node = topology.nodes[key]
        # Fluent Bit tails the json-file logs under the Docker root.
        driver = node.command(
            ("docker", "info", "--format={{.LoggingDriver}}")
        ).stdout.strip()
        if driver != "json-file":
            raise ValueError(
                "managed function collection requires Docker json-file driver"
            )
        inspected = _inspect(nodes[key], f"wb-log-collector-{key}")
        if (
            inspected["nanoCpus"] != round(logs.cpu * 1e9)
            or inspected["memoryBytes"] != logs.memory_mib * 1024 * 1024
        ):
            raise ValueError("collector resource budget mismatch")
        return {"image": inspected["image"], **collector_budget()}

    def collector_step(key: str) -> Step:
        resource, instance = collectors[key]
        host = nodes[key]
        node = topology.nodes[key]
        name = f"wb-{instance.name}"
        metrics_file = workbench_dir / host.key / instance.name / "metrics.txt"

        def run(values: Members, inputs: TaskInputs) -> None:
            # Today's teardown, step for step: bounded drain wait, graceful
            # stop, state archive, then the Loki and queue-event export. Every
            # failure is an issue in this node's status.json.
            value = values.of(resource)
            issues: list[str] = []
            final_metrics = ""
            try:
                for _ in range(DRAIN_POLLS):
                    time.sleep(DRAIN_POLL_SECONDS)
                    collect(inputs, host, instance, value)
                    final_metrics = metrics_file.read_text()
                    if collection_evidence(final_metrics)["status"] == "drained":
                        break
            except Exception as error:
                issues.append(f"collector metrics: {error}")
            identity: dict[str, Any] | None = None
            try:
                identity = {**_inspect(host, name), **collector_budget()}
            except Exception as error:
                issues.append(f"docker inspect: {error}")
            archive_issue = None
            try:
                stopped = node.provider.exec_argv(
                    node.request,
                    (
                        "docker",
                        "stop",
                        "-t",
                        str(
                            int(
                                operational_limit(settings, "process_drain_seconds", 15)
                            )
                        ),
                        name,
                    ),
                )
                if stopped.return_code:
                    raise RuntimeError("collector graceful stop failed")
                directory = run_dir / "logging" / key
                directory.mkdir(parents=True, exist_ok=True)
                archive_node_dir(
                    node, storage_dir, directory / "collector-state.tar.gz"
                )
                node.command(("sudo", "rm", "-rf", storage_dir))
            except Exception as error:
                archive_issue = str(error)
                retain_collector_for_recovery(
                    node, run_dir=run_dir, storage=storage_dir, error=error
                )
            export_collector(
                node=node,
                name=name,
                run_dir=run_dir,
                logs=export_logs,
                session=LogSession(
                    key,
                    name,
                    storage_dir,
                    int((value.deployed_at or 0) * 1e9),
                    identity or {},
                ),
                final_metrics=final_metrics,
                archive_issue=archive_issue,
                issues=issues,
            )
            if archive_issue is not None:
                raise RuntimeError(
                    f"{key}: collector state archive failed; {storage_dir} "
                    f"retained on the VM: {archive_issue}"
                )

        return run

    def loki_step(_values: Members, _inputs: TaskInputs) -> None:
        placement_file = (
            logging_dir if operator_logging is not None else run_dir
        ) / "logging-host-placement.json"
        try:
            facts = _inspect(OPERATOR, loki_container)
            facts["cpus"] = _cpu_list(facts["cpuset"])
        except Exception as error:
            facts = {"error": str(error)}
        _write_json(placement_file, {"host": "operator", **facts})
        logging_dir.mkdir(parents=True, exist_ok=True)
        try:
            log = _loki_log(loki_container)
        except Exception as error:  # e.g. TimeoutExpired: still verify below
            log = f"docker logs {loki_container} failed: {error}\n"
        (logging_dir / "backend.log").write_text(log)
        verify_loki_ingestion(
            loki_url + PUSH_PATH, f"nanolab-logs-{run_id}", logging_dir, settings
        )

    status_file = workbench_dir / "supervisor-status.json"

    def record_supervisor_status(values: Members) -> None:
        url = values.of(supervisor).url or f"http://127.0.0.1:{SUPERVISOR_PORT}"
        _write_json(status_file, SupervisorClient(url, timeout_seconds=10).status())

    def supervisor_status(values: Members, _inputs: TaskInputs) -> None:
        # A supervised run recorded it at detach, with every target still on.
        if not status_file.exists():
            record_supervisor_status(values)

    steps: list[tuple[str, Step]] = [
        # First: stopping a collector would fault its target.
        ("Record the supervisor status", supervisor_status),
        ("Snapshot the workbench Prometheus", snapshot),
        *(
            (f"Collect the {key} log collector", collector_step(key))
            for key in collectors
        ),
        *((("Export and verify Loki", loki_step),) if logs.enabled else ()),
    ]

    def step(fn: Step) -> Callable[[TaskInputs], None]:
        # Collect steps get the members' values as their upstream.
        return lambda inputs: fn(cast(Members, inputs.upstream()), inputs)

    return OneShotPlatform(
        compose(
            "Acquire the one-shot workbench platform",
            tuple(members),
            collect=tuple(call(title, step(fn)) for title, fn in steps),
        ),
        loki,
        prometheus,
        supervisor,
        {key: resource for key, (resource, _) in collectors.items()},
        collector_identity,
        targets,
        export,
        partial(loadgen_resource, OPERATOR, cpuset=operator_cpus),
        record_supervisor_status,
    )
