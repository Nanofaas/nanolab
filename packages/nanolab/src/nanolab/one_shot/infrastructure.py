"""Run-scoped Multipass resources, one VM per logical node."""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sonata_engine import Resource
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from nanolab.config import EnvironmentConfig
from nanolab.config.one_shot import OneShotConfig, OneShotNode
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
    if sum(node.cpus for node in config.nodes) > cpus:
        raise ValueError("one-shot VM CPU allocation exceeds host capacity")
    if sum(node.memory_mib for node in config.nodes) > available_memory:
        raise ValueError("one-shot VM memory allocation exceeds available host memory")
    namespace = run_id or uuid4().hex[:12]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", namespace):
        raise ValueError("invalid one-shot run namespace")
    nodes = {}
    root = repo_root or Path.cwd()
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

        def acquire(inputs, *, original=base, owner=provider, expected=request):
            if owner.vm_exists(expected):
                raise RuntimeError(f"refusing to adopt existing VM {expected.name}")
            return original.acquire(inputs)

        vm = Resource(
            title=base.title,
            acquire=acquire,
            release=base.release,
            requires=base.requires,
            revive=base.revive,
        )
        nodes[node.id] = NodeResource(node, request, vm, provider)
    generator = Resource(
        title=f"Acquire {namespace} operator-host generator",
        acquire=lambda _inputs: platform.node(),
        release=lambda _inputs, _value: None,
    )
    return OneShotResources(nodes, generator)
