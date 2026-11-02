"""Where a run executes: the provider, its machines and their settings.

`EnvironmentConfig` is the parsed environment file. Besides the provider it
carries one `RoleTarget` per execution role and whichever provider block the
selected provider needs, and it validates that the two agree.
"""

from __future__ import annotations

from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from nanolab.core.execution import ExecutionRole

ProviderName = Literal["local", "multipass", "external", "azure", "proxmox"]


class RoleTarget(BaseModel):
    """One machine a role runs on, and how to reach it.

    Every field has a default, so a role only states what differs from the
    local case; the provider validator decides which fields an environment is
    actually required to set.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    host: str | None = None
    user: str = "ubuntu"
    home: str | None = None
    kubeconfig: str | None = None
    cpus: int = Field(default=4, gt=0)
    memory: str = "12G"
    disk: str = "30G"
    hpa_scale_to_zero: bool = Field(default=False, alias="hpaScaleToZero")

    @property
    def remote_home(self) -> str:
        """Where this role's user lives on its machine.

        On the model rather than in a helper: two modules had grown their own
        copy of the rule with different signatures, and neither could be found
        from the other.
        """
        return self.home or ("/root" if self.user == "root" else f"/home/{self.user}")


class AzureEnvironment(BaseModel):
    """The Azure resources an azure-provider run provisions its machines from.

    The image URNs and VM sizes are per-role, because the release phases build
    one arm64 image on a native Ampere VM while the run itself happens on the
    amd64 pair.
    """

    model_config = ConfigDict(extra="forbid")

    resource_group: str
    location: str
    image_urn: str | None = None
    ssh_key_path: str | None = None
    operator_source_cidr: str | None = None
    priority: Literal["Regular", "Spot"] = "Regular"
    eviction_policy: Literal["Delete", "Deallocate"] | None = None
    max_bid_price: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    vm_size: str = "Standard_D4s_v5"
    loadgen_vm_size: str = "Standard_B1s"
    # Native ARM64 builder (Ampere): used by the release arm64 phases.
    arm_vm_size: str = "Standard_D8ps_v5"
    arm_image_urn: str | None = None
    # Where the OpenTofu workspace (the state a VM is deleted from) lives;
    # the SDK default is ~/.azure-vm-sdk. Used by one-shot Azure placement.
    work_dir: str | None = None
    # Architecture of `vm_size`, for one-shot placement's per-architecture images.
    architecture: Literal["arm64", "amd64"] = "amd64"


class ProxmoxEnvironment(BaseModel):
    """How to reach one Proxmox node, and which template to clone VMs from."""

    model_config = ConfigDict(extra="forbid")

    host: str
    node: str
    user: str = "root@pam"
    password_env: str = "PROXMOX_PASSWORD"
    template_id: int | None = None
    ssh_key_path: str | None = None


class MultipassSite(BaseModel):
    """A remote Multipass host that one-shot nodes can be placed on.

    `ssh` is the destination the operator runs commands through; `address` is
    the host's LAN address, on which its VMs' services are published;
    `multipass` is the client binary on that host.

    A host behind a NAT that forwards only SSH sets `tunnel`: `address` is then
    an address of the operator, where operator-owned `ssh -L` tunnels publish
    the VMs' services, and everyone (operator, peers, the site's own VMs)
    reaches them there.
    """

    model_config = ConfigDict(extra="forbid")

    ssh: str = Field(min_length=1)
    address: str
    # The VMs' architecture selects which prebuilt images they receive.
    architecture: Literal["arm64", "amd64"]
    multipass: str = "multipass"
    # Injected into the site's VMs; the operator reaches them through the site.
    vm_ssh_key: str = Field(default="~/.ssh/id_ed25519_nanolab_vm", alias="vmSshKey")
    tunnel: bool = False

    @model_validator(mode="after")
    def validate_address(self) -> MultipassSite:
        """Require a literal address: published endpoints are built from it."""
        try:
            IPv4Address(self.address)
        except ValueError as error:
            raise ValueError("site address must be an IPv4 address") from error
        return self


class OperatorLogging(BaseModel):
    """The single Loki a multi-site run owns on the operator.

    Loki runs from a pinned release binary, listening on the operator's
    loopback only; each site's VMs and the cloud reach it through a reverse
    SSH tunnel. `cpus` keeps it off the generator's CPUs.
    """

    model_config = ConfigDict(extra="forbid")

    loki_binary: str = Field(alias="lokiBinary", min_length=1)
    loki_sha256: str = Field(alias="lokiSha256", pattern=r"^[0-9a-f]{64}$")
    port: int = Field(default=43100, ge=1024, le=65534)
    cpus: list[int] = Field(min_length=1)


AZURE_SITE = "azure"


class EnvironmentConfig(BaseModel):
    """A parsed environment file: the provider, its roles and its settings.

    The validator runs at construction, so an instance that exists is one whose
    provider has everything it needs to provision: a named stack for multipass
    and external, and the matching `azure` or `proxmox` block otherwise.

    `sites` and `placement` are only used by the one-shot workflows: they put
    individual nodes on remote Multipass hosts or on Azure instead of the local
    Multipass daemon. Without them every node stays local.
    """

    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    roles: dict[ExecutionRole, RoleTarget] = Field(default_factory=dict)
    azure: AzureEnvironment | None = None
    proxmox: ProxmoxEnvironment | None = None
    sites: dict[str, MultipassSite] = Field(default_factory=dict)
    placement: dict[str, str] = Field(default_factory=dict)
    operator_logging: OperatorLogging | None = Field(default=None, alias="logging")
    containerd_maven_repository: Path | None = Field(
        default=None, alias="containerdMavenRepository"
    )
    _containerd_maven_token: str = PrivateAttr(default_factory=lambda: uuid4().hex[:12])

    @property
    def containerd_maven_token(self) -> str:
        """The identity of this environment instance's owned Maven stage."""
        return self._containerd_maven_token

    @model_validator(mode="after")
    def validate_provider(self) -> EnvironmentConfig:
        """Reject an environment whose provider is missing what it needs.

        An after-validator, so it returns the instance unchanged once every
        requirement of the selected provider is satisfied.
        """
        if self.provider == "multipass":
            stack = self.roles.get("stack")
            if stack is None or not stack.name:
                raise ValueError("stack name is required for multipass provider")
        if self.provider == "external":
            stack = self.roles.get("stack")
            if stack is None or not stack.host:
                raise ValueError("stack host is required for external provider")
            loadgen = self.roles.get("loadgen")
            if loadgen is not None and not loadgen.host:
                raise ValueError("loadgen host is required for external provider")
        if self.provider == "azure" and self.azure is None:
            raise ValueError("azure configuration is required for azure provider")
        if self.provider == "proxmox" and self.proxmox is None:
            raise ValueError("proxmox configuration is required for proxmox provider")
        if (
            self.containerd_maven_repository is not None
            and not self.containerd_maven_repository.is_absolute()
        ):
            raise ValueError("containerdMavenRepository must be an absolute host path")
        if self.sites or self.placement:
            if self.provider != "multipass":
                raise ValueError("sites and placement require the multipass provider")
            if AZURE_SITE in self.sites:
                raise ValueError(f"site name {AZURE_SITE!r} is reserved")
            if not self.sites:
                raise ValueError("placement requires at least one of the sites")
            unknown = set(self.placement.values()) - {*self.sites, AZURE_SITE}
            if unknown:
                raise ValueError(f"placement names unknown site(s): {sorted(unknown)}")
            if AZURE_SITE in self.placement.values() and self.azure is None:
                raise ValueError("azure configuration is required for azure placement")
        elif self.operator_logging is not None:
            raise ValueError("logging is only meaningful with sites and placement")
        return self

    @property
    def multisite(self) -> bool:
        """Whether nodes are placed on remote sites rather than kept local."""
        return bool(self.placement)

    def site_of(self, node_id: str) -> str | None:
        """Return the site a node is placed on, or None for the local daemon."""
        return self.placement.get(node_id)

    def target(self, role: ExecutionRole) -> RoleTarget:
        """Return the machine a role runs on, following the fallback chain.

        An unset role inherits rather than erroring: stack, cloud and
        arm-builder resolve to the host target, and loadgen falls back to the
        stack. With no host entry either, the returned `RoleTarget` carries the
        model defaults.
        """
        if role == "faasd":
            if role not in self.roles:
                raise ValueError("faasd requires a dedicated role target")
            return self.roles[role]
        if role == "host":
            return self.roles.get("host", RoleTarget())
        if role == "stack":
            return self.roles.get("stack", self.target("host"))
        if role == "cloud":
            return self.roles.get("cloud", self.target("stack"))
        if role == "arm-builder":
            return self.roles.get("arm-builder", self.target("stack"))
        return self.roles.get("loadgen", self.target("stack"))
