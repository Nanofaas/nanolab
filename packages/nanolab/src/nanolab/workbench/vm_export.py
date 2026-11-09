"""Rebuild the run's vm-resources rows from the workbench Prometheus.

node_exporter and cAdvisor are scraped every `step_seconds` with honor_timestamps
false, so a sample's timestamp is its scrape time. One row per scrape keeps the
shape the sampler wrote; `policy.json` records the scrape period, which
`summarize_resources` uses as its slot size.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from nanolab.one_shot.vm_resources import telemetry_policy
from nanolab.tasks.infra.ansible import workbench_collections_path
from nanolab.tasks.loadtest.prometheus import query_prometheus_range_series
from nanolab.tasks.one_shot.artifacts import canonical_bytes, write_immutable_artifact

_MEMORY = ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")
_LABEL = "container_label_io_nanofaas_"


@dataclass(frozen=True)
class NodeRoles:
    """One node's container name -> role map, and its observer containers."""

    roles: Mapping[str, str]
    observers: tuple[str, ...] = ()


def _points(url: str, query: str, start: datetime, end: datetime, step: int):
    """Group a range query's samples by step point."""
    grouped: dict[str, list[tuple[float, dict[str, str]]]] = defaultdict(list)
    for point in query_prometheus_range_series(
        url, query, start, end, step, preserve_labels=True
    ):
        grouped[point["timestamp"]].append((point["value"], point["labels"]))
    return grouped


def _cpu(samples: list[tuple[float, dict[str, str]]]) -> dict[str, Any]:
    """Compute the sampler's CPU fractions from per-cpu, per-mode rates."""
    modes: dict[str, float] = defaultdict(float)
    for value, labels in samples:
        modes[labels["mode"]] += value
    total = sum(modes.values())
    if total <= 0:
        return {"cpuUtilization": None}
    # Guest time is its own metric in node_exporter, so it is not double-counted.
    busy = total - modes["idle"] - modes["iowait"] - modes["steal"]
    count = len({labels["cpu"] for _value, labels in samples})
    return {
        "vcpuCount": count,
        "cpuUtilization": busy / total,
        "cpuUsedCores": count * busy / total,
        "cpuIowaitFraction": modes["iowait"] / total,
        "cpuStealFraction": modes["steal"] / total,
    }


def _memory(samples: list[tuple[float, dict[str, str]]]) -> dict[str, int]:
    values = {
        labels["__name__"].removeprefix("node_memory_").removesuffix("_bytes"): int(
            value
        )
        for value, labels in samples
    }
    if not set(_MEMORY) <= values.keys():
        return {}
    total, available, swap, free = (values[key] for key in _MEMORY)
    return {
        "memoryTotalBytes": total,
        "memoryAvailableBytes": available,
        "memoryUsedBytes": total - available,
        "swapTotalBytes": swap,
        "swapFreeBytes": free,
        "swapUsedBytes": swap - free,
    }


def _identity(labels: dict[str, str], roles: NodeRoles) -> dict[str, Any]:
    """Identify a container as the sampler did, from cAdvisor's name and labels."""
    name = labels["name"]
    role, function, replica = roles.roles.get(name, "other"), None, None
    if name not in roles.roles and labels.get(_LABEL + "managed") == "true":
        role = "function"
        function = labels[_LABEL + "function"].split("/", 1)[-1]
        replica = int(labels[_LABEL + "replica"])
    # cAdvisor's id is the cgroup path: /system.slice/docker-<id>.scope or /docker/<id>
    container = Path(labels["id"]).name.removeprefix("docker-").removesuffix(".scope")
    return {
        "containerId": container,
        "containerName": name,
        "role": role,
        "function": function,
        "replica": replica,
    }


def _containers(
    cpu: list[tuple[float, dict[str, str]]],
    memory: list[tuple[float, dict[str, str]]],
    roles: NodeRoles,
) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for samples, field in ((cpu, "cpuUsedCores"), (memory, "memoryCurrentBytes")):
        for value, labels in samples:
            identity = _identity(labels, roles)
            entry = found.setdefault(
                identity["containerId"],
                {**identity, "cpuUsedCores": None, "memoryCurrentBytes": None},
            )
            # cAdvisor may split usage per cpu; the container's cores are the sum.
            entry[field] = (entry[field] or 0) + (
                value if field == "cpuUsedCores" else int(value)
            )
    return dict(sorted(found.items()))


def _images() -> dict[str, str]:
    roles = workbench_collections_path() / "ansible_collections/nanofaas/workbench"
    return {
        component: yaml.safe_load(
            (roles / "roles" / role / "defaults/main.yml").read_text()
        )[role + "_image"]
        for component, role in (
            ("node-exporter", "node_exporter"),
            ("cadvisor", "cadvisor"),
        )
    }


def export_vm_resources(
    prometheus_url: str,
    run_dir: Path,
    *,
    nodes: Mapping[str, NodeRoles],
    start: datetime,
    end: datetime,
    step_seconds: int,
) -> None:
    """Write `vm-resources/<node>.jsonl` and `policy.json` from Prometheus."""
    directory = run_dir / "vm-resources"
    # Prometheus 3 range selectors are left-open, so a window of one scrape
    # period holds a single sample; irate over two periods is the last
    # scrape-to-scrape delta, the same interval the sampler differenced.
    window = f"[{2 * step_seconds}s]"
    for node, roles in nodes.items():
        where = f'node="{node}"'
        containers = f'{{{where},name!=""}}'

        def points(query: str) -> dict[str, list[tuple[float, dict[str, str]]]]:
            return _points(prometheus_url, query, start, end, step_seconds)

        # A failed scrape marks the series stale, so its step point is absent:
        # the gap stays a missing slot instead of repeating the last scrape.
        scrapes = points(f"timestamp(node_memory_MemTotal_bytes{{{where}}})")
        memory = points(
            f'{{__name__=~"node_memory_({"|".join(_MEMORY)})_bytes",{where}}}'
        )
        cpu = points(f"irate(node_cpu_seconds_total{{{where}}}{window})")
        container_cpu = points(
            f"irate(container_cpu_usage_seconds_total{containers}{window})"
        )
        container_memory = points(f"container_memory_usage_bytes{containers}")
        lines = []
        for step in sorted(scrapes):
            ((scraped, _labels),) = scrapes[step]
            row: dict[str, Any] = {
                "schemaVersion": 1,
                "timestampUtc": datetime.fromtimestamp(scraped, UTC).isoformat(),
                "node": node,
                "errors": [],
                **_memory(memory.get(step, [])),
                **_cpu(cpu.get(step, [])),
            }
            found = _containers(
                container_cpu.get(step, []), container_memory.get(step, []), roles
            )
            vcpus = row.get("vcpuCount")
            for entry in found.values():
                cores = entry["cpuUsedCores"]
                entry["cpuVmFraction"] = (
                    cores / vcpus if cores is not None and vcpus else None
                )
            identity = ("containerId", "containerName", "role", "function", "replica")
            row["containerInventory"] = [
                {key: entry[key] for key in identity} for entry in found.values()
            ]
            row["expectedContainerIds"] = list(found)
            row["containers"] = list(found.values())
            observers = [
                entry
                for entry in found.values()
                if entry["containerName"] in roles.observers
            ]
            cores = [
                e["cpuUsedCores"] for e in observers if e["cpuUsedCores"] is not None
            ]
            sizes = [
                e["memoryCurrentBytes"]
                for e in observers
                if e["memoryCurrentBytes"] is not None
            ]
            # Ruling 2: the observer containers stand where the sampler's own
            # cost stood; the RSS peak over a run is the max of these sums.
            row["sampler"] = {"cpuUsedCores": sum(cores) if cores else None}
            if sizes:
                row["sampler"]["memoryRssPeakBytes"] = sum(sizes)
            lines.append(canonical_bytes(row) + b"\n")
        write_immutable_artifact(directory / f"{node}.jsonl", b"".join(lines))
    policy = {
        key: value
        for key, value in telemetry_policy().items()
        if key != "samplerSha256"
    }
    policy.update(
        periodSeconds=step_seconds,
        transport="prometheus-node-exporter-cadvisor",
        phaseCorrelation="prometheus-scrape-utc",
        exporterImages=_images(),
    )
    write_immutable_artifact(directory / "policy.json", canonical_bytes(policy))
