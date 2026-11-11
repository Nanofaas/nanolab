"""Run-scoped Multipass resources, one VM per logical node."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
from collections.abc import Callable
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
from nanolab.one_shot.addresses import NodeAddresses, direct, published
from nanolab.one_shot.sites import (
    OperatorTunnel,
    SiteForwarders,
    SiteHost,
    control_path,
)
from nanolab.tasks.provisioning.providers import provider_for
from nanolab.tasks.provisioning.resources import provisioned_vm
from nanolab.tasks.resource_tasks import acquire_now, release_now, resource_of
from nanolab.tasks.vm.models import VmInfo, VmRequest


@dataclass(frozen=True)
class NodeResource:
    """An individual VM and executor; global execution roles stay unchanged."""

    config: OneShotNode
    request: VmRequest
    vm: Resource[VmInfo]
    provider: Any
    recovery: list[str] = field(default_factory=list, compare=False)
    # Set for nodes on a remote Multipass site: the site's host, on whose
    # address the node's services are published, and the node's 1-based
    # position in the topology, which selects its published ports.
    site_host: SiteHost | None = None
    ordinal: int = 0
    # Selects the node's prebuilt images when they are per architecture.
    architecture: str = field(default_factory=lambda: machine_architecture())
    # The VM's connection facts once acquired, for checks that run outside a
    # task (supervision probes).
    live: list[VmInfo] = field(default_factory=list, compare=False)

    def live_vm(self) -> VmInfo:
        """Return the acquired VM's connection facts."""
        if not self.live:
            raise RuntimeError(f"{self.config.id}: VM not acquired")
        return self.live[-1]

    def addresses(self, vm: VmInfo) -> NodeAddresses:
        """Return where operator and peers reach this node once its VM exists."""
        if self.site_host is not None:
            return published(self.site_host.site.address, self.ordinal)
        return direct(vm.host)

    def ansible_vars(self, vm: VmInfo) -> dict[str, Any]:
        """Resolve Ansible connection facts from an acquired or journalled VM."""
        from multipass_vm_sdk import find_ssh_public_key
        from sonata_tasks.vm.azure import AzureVmProvider
        from sonata_tasks.vm.proxmox import ProxmoxVmProvider
        from sonata_tasks.vm.ssh import find_ssh_private_key_path

        from nanolab.one_shot.sites import site_jump

        request = self.request.model_copy(
            update={"host": vm.host, "user": vm.user, "home": vm.home}
        )
        facts: dict[str, Any] = {"ansible_host": vm.host, "ansible_user": vm.user}
        if self.site_host is not None:
            site = self.site_host.site
            key = Path(site.vm_ssh_key).expanduser()
            facts["ansible_ssh_common_args"] = "-o " + shlex.quote(site_jump(site))
        elif isinstance(self.provider, ProxmoxVmProvider):
            facts["ansible_host"], facts["ansible_port"] = self.provider.ssh_endpoint(
                request
            )
            key = self.provider.ssh_private_key_path(request)
        elif isinstance(self.provider, AzureVmProvider):
            facts["ansible_host"] = self.provider.connection_host(request)
            key = self.provider.ssh_private_key_path(request)
        else:
            key = find_ssh_private_key_path(find_ssh_public_key())
        if key is not None:
            facts["ansible_private_key_file"] = str(key)
        if self.site_host is not None:
            # Ansible's default socket ignores the jump host and inventory alias.
            identity = json.dumps({"vm": vm.name, **facts}, sort_keys=True).encode()
            facts["ansible_control_path"] = (
                "%(directory)s/nl-" + hashlib.sha256(identity).hexdigest()[:20]
            )
        return facts

    def replica_url(self, vm: VmInfo, host_port: int) -> str:
        """Return the URL the operator reads a replica's runtime endpoints at.

        Replica ports are dynamic; behind a site's NAT or an Azure NSG only the
        in-VM probe can reach them, so their runtime requests go through its
        relay.
        """
        if self.site_host is not None or self.request.lifecycle == "azure":
            return f"{self.addresses(vm).probe}/relay/{host_port}"
        return f"http://{vm.host}:{host_port}"

    def multipass_argv(self, *args: str) -> tuple[str, ...]:
        """Return the local argv running the Multipass client that owns this VM."""
        if self.site_host is not None:
            return tuple(self.site_host.multipass_argv(*args))
        return ("multipass", *args)

    def azure_ssh(self, vm: VmInfo, *, shared: bool = True) -> tuple[str, ...]:
        """Return the ssh prefix reaching this Azure VM.

        Commands share one connection, so the run's periodic checks over the
        WAN do not each pay (and risk) a new handshake; `shared=False` opens a
        dedicated one, as a long-lived tunnel needs.
        """
        key = self.provider.ssh_private_key_path(self.request)
        multiplex = (
            (
                *("-o", "ControlMaster=auto", "-o", f"ControlPath={control_path()}"),
                *("-o", "ControlPersist=300"),
            )
            if shared
            else ("-o", "ControlMaster=no", "-o", "ControlPath=none")
        )
        return (
            "ssh",
            *("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no"),
            *("-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=15"),
            *("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"),
            *multiplex,
            *(("-i", str(key)) if key is not None else ()),
            f"{vm.user}@{vm.host}",
        )

    def reset_shared_ssh(self, vm: VmInfo) -> None:
        """Close the shared connection to an Azure VM; the next command reopens it.

        A connection keeps the groups its login had: after bootstrap adds the
        user to `docker`, only a new one can use Docker.
        """
        if self.request.lifecycle != "azure":
            return
        shared = self.azure_ssh(vm)
        subprocess.run(
            (*shared[:-1], "-O", "exit", shared[-1]),
            capture_output=True,
            timeout=30,
            check=False,
        )

    def vm_argv(self, vm: VmInfo, *argv: str) -> tuple[str, ...]:
        """Return the local argv that runs `argv` inside the VM, streaming output."""
        if self.request.lifecycle == "azure":
            return (*self.azure_ssh(vm), shlex.join(argv))
        return self.multipass_argv("exec", vm.name, "--", *argv)

    def command(self, argv: tuple[str, ...], *, remote_dir: str | None = None):
        """Execute on this node and retain transport failures as failures."""
        result = self.provider.exec_argv(self.request, argv, remote_dir=remote_dir)
        if result.return_code != 0:
            raise RuntimeError(f"{self.config.id}: {result.stderr or result.stdout}")
        return result


# One upload to an Azure VM may take this long before it is retried.
AZURE_TRANSFER_SECONDS = 600
AZURE_TRANSFER_ATTEMPTS = 3


def transfer_to_vm(node: NodeResource, vm: VmInfo, source: Path, destination: str):
    """Copy a local file into a node's VM, bounding and retrying it on Azure.

    The Azure provider uploads through the SDK's SFTP, which has no stall
    bound: in the third ten-edge run one image upload sat in TCP
    retransmission for half an hour while new connections moved 100 MB in two
    seconds. An Azure VM therefore gets OpenSSH scp on a dedicated connection,
    whose ServerAlive probes close a stalled one within a minute, each attempt
    bounded and retried on a fresh connection.
    """
    if node.request.lifecycle != "azure":
        result = node.provider.transfer_to(
            node.request, source=source, destination=destination
        )
        if result.return_code != 0:
            raise RuntimeError(f"{node.config.id}: transfer of {source.name} failed")
        return
    ssh = node.azure_ssh(vm, shared=False)
    argv = ["scp", "-q", *ssh[1:-1], str(source), f"{ssh[-1]}:{destination}"]
    failures = []
    for _attempt in range(AZURE_TRANSFER_ATTEMPTS):
        try:
            subprocess.run(
                argv,
                check=True,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=AZURE_TRANSFER_SECONDS,
            )
            return
        except subprocess.TimeoutExpired:
            failures.append(f"no completion in {AZURE_TRANSFER_SECONDS} s")
        except subprocess.CalledProcessError as error:
            failures.append((error.stderr or "").strip() or f"exit {error.returncode}")
    raise RuntimeError(
        f"{node.config.id}: transfer of {source.name} failed: " + "; ".join(failures)
    )


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
    # Multi-site placement rechecks each site on its own host; None means the
    # local `host_cpu.collect_host_placement` applies.
    recheck_placement: Callable[[dict[str, str]], dict] | None = None
    # False when nodes run on several machines (sites, Azure).
    shared_physical_host: bool = True

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
    nodes: list[OneShotNode] | None = None,
    logging_backend: bool = True,
) -> dict[str, float]:
    """Require disjoint VM, operator and owned backend budgets before acquisition.

    MemAvailable already reflects running external VMs, so do not subtract twice.
    Collector resources fit inside each node and are validated by the config.
    `nodes` narrows the budget to the VMs of one host (default: all of them).
    """
    nodes = config.nodes if nodes is None else nodes
    backend = (
        logging_backend
        and config.observability.logs.enabled
        and config.observability.logs.backend_owned
    )
    cpu_required = (
        sum(node.cpus for node in nodes)
        + external_cpus
        + reserve_cpus
        + (1.5 if backend else 0)
    )
    memory_required = (
        sum(node.memory_mib for node in nodes)
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


def machine_architecture(machine: str | None = None) -> str:
    """Return the image architecture name of a kernel machine type."""
    machine = machine or platform.machine()
    names = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    if machine not in names:
        raise ValueError(f"unsupported machine architecture: {machine}")
    return names[machine]


def _owned_node(
    node: OneShotNode,
    request: VmRequest,
    provider: Any,
    *,
    config: OneShotConfig,
    generator: Resource[str],
    target_cpus: list[int] | None,
    pin: Callable[[str, list[int]], object],
    stop_argv: Callable[[str], tuple[str, ...]] | None,
    after_ensure: Callable[[VmInfo], None] | None = None,
    site_host: SiteHost | None = None,
    ordinal: int = 0,
    architecture: str | None = None,
    tunnel_journal: Path | None = None,
    acquired_vm: Resource[VmInfo] | None = None,
) -> NodeResource:
    """Own one node's VM: refuse adoption, pin it, retain it for raw recovery."""
    # Ownership is checked at acquire time, including partial launch compensation.
    base = (
        acquired_vm
        if acquired_vm is not None
        else provisioned_vm(
            title=f"Acquire {node.id} VM",
            request=request,
            provider=provider,
            after_ensure=after_ensure,
        )
    )
    recovery: list[str] = []
    live: list[VmInfo] = []
    # A node behind a site's NAT is published on the site's address for as
    # long as its VM exists.
    forwarders = SiteForwarders(site_host) if site_host is not None else None
    # Behind a NAT that forwards only SSH, the operator publishes them instead.
    addresses = (
        published(site_host.site.address, ordinal) if site_host is not None else None
    )
    tunnel = (
        OperatorTunnel(
            site_host.site,
            site_host.site.address,
            tuple(port for port, _ in addresses.forwards()),
            name=node.id,
            journal=tunnel_journal,
        )
        if site_host is not None and addresses is not None and site_host.site.tunnel
        else None
    )

    def unpublish():
        try:
            if tunnel is not None:
                tunnel.stop()
        finally:
            if forwarders is not None:
                forwarders.stop()

    def acquire(inputs):
        if acquired_vm is None and provider.vm_exists(request):
            raise RuntimeError(f"refusing to adopt existing VM {request.name}")
        value = acquire_now(base, inputs)
        try:
            if target_cpus is not None:
                if request.name is None:
                    raise ValueError("owned VM name missing for host placement")
                pin(request.name, target_cpus)
            if forwarders is not None and addresses is not None:
                forwarders.start(value.host, addresses.forwards())
            if tunnel is not None:
                tunnel.start()
        except BaseException as error:
            try:
                unpublish()
                release_now(base, inputs, value)
            except Exception as cleanup:
                error.add_note(f"owned VM placement compensation: {cleanup}")
            raise
        live.append(value)
        return value

    def release(inputs, value):
        unpublish()
        if recovery:
            if request.name is None:
                raise RuntimeError("owned VM name missing for raw recovery")
            if stop_argv is not None:
                subprocess.run(
                    stop_argv(request.name),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=operational_limit(config, "process_exit_seconds", 30),
                )
            raise RuntimeError(
                f"owned VM {request.name} stopped and retained for raw recovery: "
                + "; ".join(recovery)
            )
        release_now(base, inputs, value)

    vm = resource_of(
        base.title,
        acquire,
        release,
        requires=(*base.requires, generator)
        if target_cpus is not None
        else base.requires,
        revive=base.revive,
        idempotent=base.acquire.idempotent,
    )
    return NodeResource(
        node,
        request,
        vm,
        provider,
        recovery,
        site_host=site_host,
        ordinal=ordinal,
        architecture=architecture or machine_architecture(),
        live=live,
    )


def _multipass_request(namespace: str, node: OneShotNode) -> VmRequest:
    return VmRequest(
        lifecycle="multipass",
        name=f"nl-os-{namespace}-{node.id}",
        cpus=node.cpus,
        memory=f"{node.memory_mib}M",
        disk=f"{node.disk_gib}G",
    )


def build_one_shot_resources(
    config: OneShotConfig,
    environment: EnvironmentConfig,
    *,
    run_id: str | None = None,
    repo_root: Path | None = None,
    run_dir: Path | None = None,
) -> OneShotResources:
    """Reuse Sonata's compensated VM lifecycle without adopting operator VMs."""
    if environment.provider != config.provider:
        raise ValueError("one-shot environment must use Multipass")
    namespace = run_id or uuid4().hex[:12]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", namespace):
        raise ValueError("invalid one-shot run namespace")
    root = repo_root or Path.cwd()
    if environment.multisite:
        from nanolab.one_shot.multisite import build_multisite_resources

        return build_multisite_resources(
            config, environment, namespace=namespace, repo_root=root, run_dir=run_dir
        )
    cpus, available_memory = host_capacity()
    check_host_capacity(
        config,
        config.nodes,
        cpus=cpus,
        available_memory=available_memory,
        external_cpus=external_vm_cpus
        if config.purpose == "scientific-experiment"
        else None,
    )
    placement = config.host_cpu_placement
    facts = host_cpu.validate_host_placement(placement) if placement else None
    generator = operator_generator(namespace, placement)
    requests = {node.id: _multipass_request(namespace, node) for node in config.nodes}
    providers = {key: provider_for(request, root) for key, request in requests.items()}
    nodes: dict[str, NodeResource] = {}
    bindings = {}
    if environment.provisioning == "opentofu":
        from nanolab.one_shot.terraform import prepare_fleet

        if run_dir is None:
            raise ValueError(
                "OpenTofu provisioning requires a persistent run directory"
            )
        bindings = prepare_fleet(
            config=config,
            environment=environment,
            namespace=namespace,
            name="local",
            requests=requests,
            provider=next(iter(providers.values())),
            run_dir=run_dir,
            resources=nodes,
        )
    nodes.update(
        {
            node.id: _owned_node(
                node,
                request,
                providers[node.id],
                config=config,
                generator=generator,
                target_cpus=placement.nodes[node.id].cpus if placement else None,
                pin=host_cpu.pin_vm,
                stop_argv=lambda name: ("multipass", "stop", name),
                acquired_vm=bindings.get(node.id),
            )
            for node in config.nodes
            for request in (requests[node.id],)
        }
    )
    return OneShotResources(
        nodes, generator, host_cpu_placement=placement, host_cpu_facts=facts
    )


def check_host_capacity(
    config: OneShotConfig,
    nodes: list[OneShotNode],
    *,
    cpus: int,
    available_memory: int,
    external_cpus: Callable[[], int] | None,
    logging_backend: bool = True,
) -> None:
    """Refuse VMs (and an owned log backend) that do not fit one host."""
    if external_cpus is not None:
        host_budget(
            config,
            nodes=nodes,
            host_cpus=cpus,
            available_memory_mib=available_memory,
            external_cpus=external_cpus(),
            logging_backend=logging_backend,
        )
    if sum(node.cpus for node in nodes) > cpus:
        raise ValueError("one-shot VM CPU allocation exceeds host capacity")
    if sum(node.memory_mib for node in nodes) > available_memory:
        raise ValueError("one-shot VM memory allocation exceeds available host memory")
    logs = config.observability.logs
    if logging_backend and logs.enabled and logs.backend_owned:
        if sum(node.cpus for node in nodes) + 1.5 > cpus:
            raise ValueError("owned logging backend CPU budget exceeds host capacity")
        if sum(node.memory_mib for node in nodes) + 768 > available_memory:
            raise ValueError("owned backend memory budget exceeds host capacity")


def operator_generator(
    namespace: str, placement: HostCpuPlacement | None
) -> Resource[str]:
    """Own the operator host's generator slot and, if placed, its CPU pinning."""
    operator_state = {}

    def acquire_operator(_inputs):
        if placement:
            operator_state["original"] = host_cpu.pin_operator(placement.operator_cpus)
        return platform.node()

    def release_operator(_inputs, _value):
        if "original" in operator_state:
            host_cpu.restore_operator(operator_state.pop("original"))

    return resource_of(
        f"Acquire {namespace} operator-host generator",
        acquire_operator,
        release_operator,
    )
