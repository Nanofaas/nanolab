"""One-shot nodes spread over remote Multipass sites and Azure.

Built only when the environment places nodes (`placement`); otherwise the
local path in `infrastructure` is used unchanged. Each site is checked and
pinned against its own host; the operator keeps its generator and CPU pins.
"""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from azure_vm import AzureClient
from sonata_tasks.vm.models import VmRequest as SonataVmRequest
from sonata_tasks.vm.providers.azure import AzureVmProvider

from nanolab.config import EnvironmentConfig
from nanolab.config.environment import AZURE_SITE
from nanolab.config.one_shot import HostCpuPlacement, OneShotConfig, OneShotNode
from nanolab.one_shot import host_cpu
from nanolab.one_shot.addresses import (
    API_PORT,
    CADVISOR_PORT,
    MANAGEMENT_PORT,
    NODE_EXPORTER_PORT,
    PROBE_PORT,
)
from nanolab.one_shot.infrastructure import (
    NodeResource,
    OneShotResources,
    _multipass_request,
    _owned_node,
    check_host_capacity,
    operator_generator,
)
from nanolab.one_shot.site_cpu import (
    inspect_site_vm,
    pin_site_vm,
    validate_site_placement,
)
from nanolab.one_shot.sites import SiteHost, SiteVmOrchestrator
from nanolab.tasks.vm.models import VmRequest

# The cloud's NSG: the operator drives and observes it, the edges' sites
# offload to it and measure bandwidth from its probe. The operator's
# Prometheus scrapes its exporters. SSH keeps the SDK's own rule.
CLOUD_OPERATOR_PORTS = (
    API_PORT,
    MANAGEMENT_PORT,
    PROBE_PORT,
    NODE_EXPORTER_PORT,
    CADVISOR_PORT,
)
CLOUD_SITE_PORTS = (API_PORT, PROBE_PORT)
AZURE_MIN_DISK_GIB = 64


class AzureSiteProvider(AzureVmProvider):
    """Sonata's Azure provider: own workspace, resource group only read."""

    def __init__(self, repo_root: Path, *, work_dir: str | None) -> None:
        """Keep the workspace (state needed to delete the VM) under `work_dir`."""
        super().__init__(repo_root, remote_project_name="nanofaas")
        self.work_dir = work_dir

    def _client(self, request: SonataVmRequest) -> AzureClient:
        private_key = self.ssh_private_key_path(request)
        return AzureClient(
            resource_group=request.azure_resource_group,
            location=request.azure_location,
            ssh_key_path=str(private_key) if private_key is not None else None,
            ssh_username=request.user,
            work_dir=str(Path(self.work_dir).expanduser()) if self.work_dir else None,
            # The resource group is shared with unrelated VMs: read it, never
            # put it in OpenTofu state, where a changed template could replace it.
            manage_resource_group=False,
        )

    def vm_exists(self, request: SonataVmRequest) -> bool:
        """Ask Azure, not the local workspace, whether the VM already exists."""
        return self._exists_in_azure(request)


def egress_address(run: Callable[[list[str]], str]) -> str:
    """Return the public address a host's outbound connections come from."""
    address = run(["curl", "-fsS", "--max-time", "10", "https://api.ipify.org"])
    return address.strip()


def cloud_ingress(
    operator: str, sites: list[str], *, extra_ports: tuple[int, ...] = ()
) -> dict[int, tuple[str, ...]]:
    """Return the cloud's inbound ports, each with the hosts allowed to reach it."""
    hosts = {port: {operator} for port in (*CLOUD_OPERATOR_PORTS, *extra_ports)}
    for port in CLOUD_SITE_PORTS:
        hosts.setdefault(port, set()).update(sites)
    return {
        port: tuple(f"{address}/32" for address in sorted(addresses))
        for port, addresses in sorted(hosts.items())
    }


def _azure_request(
    namespace: str,
    node: OneShotNode,
    environment: EnvironmentConfig,
    ingress: dict[int, tuple[str, ...]],
) -> VmRequest:
    azure = environment.azure
    if azure is None:
        raise ValueError("azure configuration is required for azure placement")
    return VmRequest(
        lifecycle="azure",
        name=f"nl-os-{namespace}-{node.id}",
        user="azureuser",
        cpus=node.cpus,
        memory=f"{node.memory_mib}M",
        # Azure refuses an OS disk smaller than the image's (30 GB for Ubuntu).
        disk=f"{max(node.disk_gib, AZURE_MIN_DISK_GIB)}G",
        azure_vm_size=azure.vm_size,
        azure_resource_group=azure.resource_group,
        azure_location=azure.location,
        azure_image_urn=azure.image_urn,
        azure_ssh_key_path=str(Path(azure.ssh_key_path).expanduser())
        if azure.ssh_key_path
        else None,
        azure_priority=azure.priority,
        azure_eviction_policy=azure.eviction_policy,
        azure_max_bid_price=azure.max_bid_price,
        # Declared at creation: the SDK renders the rules into its NSG.
        azure_ingress=ingress,
    )


def _validate_placement(
    config: OneShotConfig,
    environment: EnvironmentConfig,
    by_site: dict[str, list[OneShotNode]],
) -> None:
    if set(environment.placement) != {node.id for node in config.nodes}:
        raise ValueError("placement must name exactly the scenario's nodes")
    if any(node.kind != "cloud" for node in by_site.get(AZURE_SITE, [])):
        raise ValueError("only the terminal cloud can be placed on Azure")
    placement = config.host_cpu_placement
    if placement is None:
        return
    for site, members in by_site.items():
        pinned = [node.id for node in members if node.id in placement.nodes]
        if site == AZURE_SITE and pinned:
            raise ValueError("Azure nodes cannot have host CPU placement")
        if site != AZURE_SITE and len(pinned) != len(members):
            raise ValueError(f"every node on {site} needs host CPU placement")
        if placement.hosts and any(
            placement.hosts.get(node) != site for node in pinned
        ):
            raise ValueError(f"host CPU placement names another host for {site}")


def build_multisite_resources(
    config: OneShotConfig,
    environment: EnvironmentConfig,
    *,
    namespace: str,
    repo_root: Path,
    run_dir: Path | None = None,
) -> OneShotResources:
    """Own every node on its placed site; check each site against its own host.

    Tunnelled sites journal their operator tunnels in `run_dir`/tunnels.jsonl.
    """
    by_site: dict[str, list[OneShotNode]] = {}
    for node in config.nodes:
        site = environment.site_of(node.id)
        if site is None:
            raise ValueError(f"placement must name every node; missing {node.id}")
        by_site.setdefault(site, []).append(node)
    _validate_placement(config, environment, by_site)
    placement = config.host_cpu_placement
    ordinals = {node.id: index for index, node in enumerate(config.nodes, start=1)}
    facts: dict[str, Any] | None = None
    if placement is not None:
        # The run's Loki shares the operator with the generator, on its own CPUs;
        # the scenario's logging CPUs belonged to the single host's Loki.
        loki = environment.operator_logging
        if loki is not None and set(loki.cpus) & set(placement.operator_cpus):
            raise ValueError("operator Loki CPUs overlap the generator's CPUs")
        facts = {
            "operator": host_cpu.validate_host_placement(
                HostCpuPlacement.model_construct(
                    nodes={},
                    operator_cpus=placement.operator_cpus,
                    logging_cpus=list(loki.cpus) if loki else [],
                )
            )
        }
    generator = operator_generator(namespace, placement)
    scientific = config.purpose == "scientific-experiment"
    hosts: dict[str, SiteHost] = {}
    nodes: dict[str, NodeResource] = {}
    for site, members in by_site.items():
        if site == AZURE_SITE:
            continue
        host = hosts[site] = SiteHost(environment.sites[site], site)
        cpus, memory = host.capacity()
        check_host_capacity(
            config,
            members,
            cpus=cpus,
            available_memory=memory,
            external_cpus=host.external_vm_cpus if scientific else None,
            # The run's Loki is on the operator, not on any site.
            logging_backend=False,
        )
        if placement is not None and facts is not None:
            facts[site] = validate_site_placement(
                host,
                {node.id: placement.nodes[node.id] for node in members},
                [],
            )
        provider = SiteVmOrchestrator(repo_root, host.site)
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
                name=site,
                requests={
                    node.id: _multipass_request(namespace, node) for node in members
                },
                provider=provider,
                run_dir=run_dir,
                resources=nodes,
                site=host.site,
            )
        for node in members:
            nodes[node.id] = _owned_node(
                node,
                _multipass_request(namespace, node),
                provider,
                config=config,
                generator=generator,
                target_cpus=placement.nodes[node.id].cpus if placement else None,
                pin=lambda name, cpus, host=host: pin_site_vm(host, name, cpus),
                stop_argv=lambda name, host=host: tuple(
                    host.multipass_argv("stop", name)
                ),
                site_host=host,
                acquired_vm=bindings.get(node.id),
                ordinal=ordinals[node.id],
                architecture=host.site.architecture,
                tunnel_journal=run_dir / "tunnels.jsonl" if run_dir else None,
            )
    azure_nodes = by_site.get(AZURE_SITE, [])
    if azure_nodes:
        azure = environment.azure
        assert azure is not None  # nosec B101 - validated by EnvironmentConfig
        operator = egress_address(_local_output)
        sites = sorted(
            {
                egress_address(lambda argv, h=h: h.run(argv).stdout)
                for h in hosts.values()
            }
        )
        ingress = cloud_ingress(operator, sites)
        for node in azure_nodes:
            nodes[node.id] = _owned_node(
                node,
                _azure_request(namespace, node, environment, ingress),
                AzureSiteProvider(repo_root, work_dir=azure.work_dir),
                config=config,
                generator=generator,
                target_cpus=None,
                pin=_no_pin,
                stop_argv=None,
                architecture=azure.architecture,
            )
    ordered = {node.id: nodes[node.id] for node in config.nodes}
    return OneShotResources(
        ordered,
        generator,
        host_cpu_placement=placement,
        host_cpu_facts=facts,
        shared_physical_host=len(by_site) == 1 and AZURE_SITE not in by_site,
        recheck_placement=_recheck(placement, hosts, by_site)
        if placement is not None
        else None,
    )


def _local_output(argv: list[str]) -> str:
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=30
    ).stdout


def _no_pin(_name: str, _cpus: list[int]) -> None:
    raise RuntimeError("Azure nodes are never pinned")


def _recheck(
    placement: HostCpuPlacement,
    hosts: dict[str, SiteHost],
    by_site: dict[str, list[OneShotNode]],
) -> Callable[[dict[str, str]], dict]:
    def recheck(vm_names: dict[str, str]) -> dict:
        operator = {
            str(tid): sorted(os.sched_getaffinity(tid))
            for tid in (int(t.name) for t in Path("/proc/self/task").iterdir())
        }
        if not operator or any(
            set(mask) != set(placement.operator_cpus) for mask in operator.values()
        ):
            raise ValueError("operator thread affinity differs from placement")
        sites = {}
        for site, host in hosts.items():
            members = {node.id: placement.nodes[node.id] for node in by_site[site]}
            sites[site] = {
                "hardware": validate_site_placement(host, members, []),
                "nodes": {
                    node: inspect_site_vm(host, vm_names[node], target.cpus)
                    for node, target in members.items()
                },
            }
        return {
            "policy": placement.model_dump(by_alias=True),
            "operatorHost": platform.node(),
            "operatorThreadMasks": operator,
            "sites": sites,
        }

    return recheck
