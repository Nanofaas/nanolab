"""Run-scoped Multipass resources, one VM per logical node."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from sonata_engine import Resource
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from nanolab.config import EnvironmentConfig
from nanolab.config.one_shot import (
    HostCpuPlacement,
    OneShotConfig,
    OneShotNode,
    operational_limit,
)
from nanolab.one_shot import host_cpu
from nanolab.tasks.provisioning.providers import provider_for
from nanolab.tasks.provisioning.resources import provisioned_vm
from nanolab.tasks.vm.models import VmInfo, VmRequest


@dataclass(frozen=True)
class NodeResource:
    """An individual VM and executor; global execution roles stay unchanged."""

    config: OneShotNode
    request: VmRequest
    vm: Resource[VmInfo]
    provider: Any
    recovery: list[str] = field(default_factory=list, compare=False)

    def command(self, argv: tuple[str, ...], *, remote_dir: str | None = None):
        """Execute on this node and retain transport failures as failures."""
        result = self.provider.exec_argv(self.request, argv, remote_dir=remote_dir)
        if result.return_code != 0:
            raise RuntimeError(f"{self.config.id}: {result.stderr or result.stdout}")
        return result


class NodeExecutor:
    """Bind reusable platform command tasks to one explicit VM."""

    def __init__(self, node: NodeResource) -> None:
        """Store the node binding without creating a VM."""
        self.node = node

    def binding_key(self, role: str) -> str:
        """Identify the explicit node independently of the global role name."""
        return f"multipass:{self.node.request.name}"

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        """Run a reusable command task in this node namespace."""
        result = self.node.provider.exec_argv(
            self.node.request,
            task.argv,
            env=dict(task.options.env),
            remote_dir=task.options.remote_dir
            or (str(task.options.cwd) if task.options.cwd else None),
            dry_run=dry_run,
        )
        return TaskResult(
            task_id=task.task_id,
            status="passed"
            if result.return_code in task.options.expected_exit_codes
            else "failed",
            return_code=result.return_code,
            expected_exit_codes=task.options.expected_exit_codes,
            stdout=result.stdout,
            stderr=result.stderr,
        )


@dataclass(frozen=True)
class OneShotResources:
    """All independently owned node resources and the declared generator location."""

    nodes: dict[str, NodeResource]
    generator: Resource[str]
    generator_location: str = "operator-host"
    host_cpu_placement: HostCpuPlacement | None = None
    host_cpu_facts: dict | None = None

    @property
    def vms(self) -> tuple[Resource[VmInfo], ...]:
        """Return VM resources in declared topology order."""
        return tuple(node.vm for node in self.nodes.values())


def host_capacity() -> tuple[int, int]:
    """Read available Linux host resources before provisioning new VMs."""
    memory = {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
    }
    return os.cpu_count() or 1, memory["MemAvailable"] // 1024


def host_budget(
    config: OneShotConfig,
    *,
    host_cpus: int,
    available_memory_mib: int,
    external_cpus: int = 0,
    reserve_cpus: float = 1,
    reserve_memory_mib: int = 1024,
) -> dict[str, float]:
    """Require disjoint VM, operator and owned backend budgets before acquisition.

    MemAvailable already reflects running external VMs, so do not subtract twice.
    Collector resources fit inside each node and are validated by the config.
    """
    backend = (
        config.observability.logs.enabled and config.observability.logs.backend_owned
    )
    cpu_required = (
        sum(node.cpus for node in config.nodes)
        + external_cpus
        + reserve_cpus
        + (1.5 if backend else 0)
    )
    memory_required = (
        sum(node.memory_mib for node in config.nodes)
        + reserve_memory_mib
        + (768 if backend else 0)
    )
    if cpu_required > host_cpus:
        raise ValueError("campaign and external VM CPU budgets exceed host capacity")
    if memory_required > available_memory_mib:
        raise ValueError("campaign memory budget exceeds available host memory")
    return {"cpuRequired": cpu_required, "memoryRequiredMiB": memory_required}


def external_vm_cpus() -> int:
    """Inventory running VM allocations without adopting or changing them."""
    result = subprocess.run(
        ("multipass", "info", "--format", "json"),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    info = json.loads(result.stdout)
    if info.get("errors"):
        raise RuntimeError("external VM inventory is incomplete")
    return sum(
        int(vm["cpu_count"]) for vm in info["info"].values() if vm["state"] == "Running"
    )


def build_one_shot_resources(
    config: OneShotConfig,
    environment: EnvironmentConfig,
    *,
    run_id: str | None = None,
    repo_root: Path | None = None,
) -> OneShotResources:
    """Reuse Sonata's compensated VM lifecycle without adopting operator VMs."""
    if environment.provider != config.provider:
        raise ValueError("one-shot environment must use Multipass")
    cpus, available_memory = host_capacity()
    if config.purpose == "scientific-experiment":
        host_budget(
            config,
            host_cpus=cpus,
            available_memory_mib=available_memory,
            external_cpus=external_vm_cpus(),
        )
    if sum(node.cpus for node in config.nodes) > cpus:
        raise ValueError("one-shot VM CPU allocation exceeds host capacity")
    if sum(node.memory_mib for node in config.nodes) > available_memory:
        raise ValueError("one-shot VM memory allocation exceeds available host memory")
    if config.observability.logs.enabled and config.observability.logs.backend_owned:
        if sum(node.cpus for node in config.nodes) + 1.5 > cpus:
            raise ValueError("owned logging backend CPU budget exceeds host capacity")
        if sum(node.memory_mib for node in config.nodes) + 768 > available_memory:
            raise ValueError("owned backend memory budget exceeds host capacity")
    namespace = run_id or uuid4().hex[:12]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", namespace):
        raise ValueError("invalid one-shot run namespace")
    nodes = {}
    root = repo_root or Path.cwd()
    placement = config.host_cpu_placement
    facts = host_cpu.validate_host_placement(placement) if placement else None
    operator_state = {}

    def acquire_operator(_inputs):
        if placement:
            operator_state["original"] = host_cpu.pin_operator(placement.operator_cpus)
        return platform.node()

    def release_operator(_inputs, _value):
        if "original" in operator_state:
            host_cpu.restore_operator(operator_state.pop("original"))

    generator = Resource(
        title=f"Acquire {namespace} operator-host generator",
        acquire=acquire_operator,
        release=release_operator,
    )
    for node in config.nodes:
        request = VmRequest(
            lifecycle="multipass",
            name=f"nl-os-{namespace}-{node.id}",
            cpus=node.cpus,
            memory=f"{node.memory_mib}M",
            disk=f"{node.disk_gib}G",
        )
        provider: Any = provider_for(request, root)
        # Ownership is checked at acquire time, including partial launch compensation.
        base = provisioned_vm(
            title=f"Acquire {node.id} VM", request=request, provider=provider
        )

        target_cpus = placement.nodes[node.id].cpus if placement else None
        recovery: list[str] = []

        def acquire(
            inputs, *, original=base, owner=provider, expected=request, cpus=target_cpus
        ):
            if owner.vm_exists(expected):
                raise RuntimeError(f"refusing to adopt existing VM {expected.name}")
            value = original.acquire(inputs)
            try:
                if cpus is not None:
                    if expected.name is None:
                        raise ValueError("owned VM name missing for host placement")
                    host_cpu.pin_vm(expected.name, cpus)
            except BaseException as error:
                try:
                    original.release(inputs, value)
                except Exception as cleanup:
                    error.add_note(f"owned VM placement compensation: {cleanup}")
                raise
            return value

        def release(
            inputs, value, *, original=base, expected=request, retained=recovery
        ):
            if retained:
                if expected.name is None:
                    raise RuntimeError("owned VM name missing for raw recovery")
                subprocess.run(
                    ("multipass", "stop", expected.name),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=operational_limit(config, "process_exit_seconds", 30),
                )
                raise RuntimeError(
                    f"owned VM {expected.name} stopped and retained for raw recovery: "
                    + "; ".join(retained)
                )
            original.release(inputs, value)

        vm = Resource(
            title=base.title,
            acquire=acquire,
            release=release,
            requires=(*base.requires, generator) if placement else base.requires,
            revive=base.revive,
        )
        nodes[node.id] = NodeResource(node, request, vm, provider, recovery)
    return OneShotResources(
        nodes, generator, host_cpu_placement=placement, host_cpu_facts=facts
    )
