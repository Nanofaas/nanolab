"""Strict configuration of the independent local one-shot workflows."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class ArtifactReference(BaseModel):
    """An immutable prerequisite whose bytes must match the recorded hash."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def read_verified(self) -> bytes:
        """Reject altered prerequisites before contacting any provider."""
        content = self.path.read_bytes()
        if hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError(f"artifact hash mismatch: {self.path}")
        return content


class NodeFunctionLimits(BaseModel):
    """Only resource limits vary between nodes; input and coefficients stay shared."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cpu: float = Field(gt=0)
    memory_mib: int = Field(alias="memoryMiB", gt=0)
    max_replicas: int = Field(alias="maxReplicas", gt=0)


CpuId = Annotated[int, Field(strict=True, ge=0)]


class HostCpuTarget(BaseModel):
    """An explicit homogeneous host CPU family for one VM."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cpus: list[CpuId] = Field(min_length=1)
    max_frequency_khz: int = Field(alias="maxFrequencyKHz", strict=True, gt=0)


class HostCpuPlacement(BaseModel):
    """Disjoint VM, operator and owned logging CPU reservations."""

    model_config = ConfigDict(extra="forbid")
    nodes: dict[str, HostCpuTarget] = Field(min_length=1)
    operator_cpus: list[CpuId] = Field(alias="operatorCpus", min_length=1)
    logging_cpus: list[CpuId] = Field(default_factory=list, alias="loggingCpus")

    @model_validator(mode="after")
    def validate_disjoint_cpus(self) -> Self:
        """Reject overlap within or between any declared reservations."""
        ids = [cpu for node in self.nodes.values() for cpu in node.cpus]
        ids.extend(self.operator_cpus)
        ids.extend(self.logging_cpus)
        if len(ids) != len(set(ids)):
            raise ValueError("host CPU reservations overlap")
        return self


class OneShotNode(BaseModel):
    """A separate Multipass VM for one logical edge or terminal cloud."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    kind: Literal["edge", "cloud"]
    cpus: int = Field(default=2, gt=0)
    memory_mib: int = Field(default=4096, alias="memoryMiB", gt=0)
    disk_gib: int = Field(default=20, alias="diskGiB", gt=0)
    memory_capacity_mib: int = Field(default=256, alias="memoryCapacityMiB", gt=0)
    function_limits: dict[str, NodeFunctionLimits] = Field(
        default_factory=dict, alias="functionLimits"
    )


class OneShotFunction(BaseModel):
    """Declared input, resource limits and model coefficients of a function."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    input: dict[str, JsonValue]
    cpu: float = Field(default=1, gt=0)
    memory_mib: int = Field(default=128, alias="memoryMiB", gt=0)
    max_replicas: int = Field(default=2, alias="maxReplicas", gt=0)
    utilization: float = Field(default=0.8, gt=0, le=1)
    alpha: float = Field(default=1, ge=0)
    delta: float = Field(default=0.9, ge=0)
    gamma: float = Field(default=0.1, ge=0)


class CalibrationSettings(BaseModel):
    """Bounded independent measurement and model validation settings."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min_samples: int = Field(default=24, alias="minSamples", ge=2)
    max_samples: int = Field(default=96, alias="maxSamples", ge=2, le=10000)
    repetitions: int = Field(default=2, ge=1, le=100)
    warmup_invocations: int = Field(default=3, alias="warmupInvocations", ge=1)
    relative_ci: float = Field(default=0.15, alias="relativeCI", gt=0, lt=1)
    confidence: float = Field(default=0.95, gt=0, lt=1)
    capacity_error: float = Field(default=0.35, alias="capacityError", gt=0, lt=1)
    capacity_seconds: float = Field(default=4, alias="capacitySeconds", gt=0)
    timeout_seconds: float = Field(default=10, alias="timeoutSeconds", gt=0)
    quantiles: list[float] = Field(default_factory=lambda: [0.5, 0.95], min_length=1)

    @model_validator(mode="after")
    def validate_sample_bounds(self) -> Self:
        """Require feasible bounded sample counts in each independent repetition."""
        if self.min_samples > self.max_samples:
            raise ValueError("minSamples exceeds maxSamples")
        if any(not 0 < value <= 1 for value in self.quantiles):
            raise ValueError("invalid service quantiles")
        return self


class ProtocolSettings(BaseModel):
    """Explicit wall-time limits passed to the NanoFaaS negotiation contract."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    auction_seconds: float = Field(default=0.6, alias="auctionSeconds", gt=0)
    peer_seconds: float = Field(default=0.15, alias="peerSeconds", gt=0)
    solver_seconds: float = Field(default=0.15, alias="solverSeconds", gt=0)
    preparation_seconds: float = Field(default=0.3, alias="preparationSeconds", gt=0)
    max_rounds: int = Field(default=8, alias="maxRounds", ge=1, le=1000)
    parallelism: int = Field(default=4, ge=1, le=64)
    max_peers: int = Field(default=16, alias="maxPeers", ge=1, le=64)
    queue_capacity: int = Field(default=64, alias="queueCapacity", ge=4, le=256)
    max_solver_states: int = Field(default=2000000, alias="maxSolverStates", ge=1)
    max_solver_bytes: int = Field(default=67108864, alias="maxSolverBytes", ge=1)

    @model_validator(mode="after")
    def validate_queue_bound(self) -> Self:
        """Preserve the protocol's four-phase peer queue bound."""
        if self.queue_capacity < 4 * self.max_peers:
            raise ValueError("queueCapacity must cover four phases per peer")
        return self


class TimingCell(BaseModel):
    """A reproducible workload cell on the declared measured local network."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    rates: dict[str, dict[str, float]] = Field(min_length=1)
    network: Literal["observed-local"] = "observed-local"

    @model_validator(mode="after")
    def validate_rates(self) -> Self:
        """Reject undefined rates and empty/no-load qualification cells."""
        values = [
            rate for functions in self.rates.values() for rate in functions.values()
        ]
        if not values or any(rate < 0 for rate in values) or not any(values):
            raise ValueError("timing cell needs positive load and nonnegative rates")
        return self


class TimingSettings(BaseModel):
    """Independent timing experiment; no implicit period or trace resolution."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    period_candidates: list[float] = Field(
        alias="periodCandidatesSeconds", min_length=1
    )
    max_trace_resolution: float = Field(alias="maxTraceResolutionSeconds", gt=0)
    minimum_samples: int = Field(default=10, alias="minSamples", ge=10, le=10000)
    quantile: float = Field(default=0.9, gt=0, lt=1)
    margin_seconds: float = Field(default=0.2, alias="marginSeconds", ge=0)
    ready_margin_seconds: float = Field(default=0.3, alias="readyMarginSeconds", gt=0)
    epsilon: float = Field(default=0.1, gt=0, le=0.2)
    lead_seconds: float = Field(default=2, alias="measurementLeadSeconds", gt=0)
    max_censored_fraction: Literal[0] = Field(default=0, alias="maxCensoredFraction")
    protocol: ProtocolSettings = Field(default_factory=ProtocolSettings)
    matrix: list[TimingCell] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        """Require enough observations to resolve the declared empirical quantile."""
        if any(not value > 0 for value in self.period_candidates):
            raise ValueError("period candidates must be positive")
        if self.minimum_samples < math.ceil(1 / (1 - self.quantile) - 1e-9):
            raise ValueError("too few samples to resolve declared timing quantile")
        if len({cell.id for cell in self.matrix}) != len(self.matrix):
            raise ValueError("duplicate timing matrix cell")
        return self


class ExperimentWindow(TimingCell):
    """A phase whose load persists for an explicit number of epochs."""

    epochs: int = Field(default=1, ge=1, le=1000)


class ExperimentSettings(BaseModel):
    """Explicit comparison windows, repetitions and generator capacity."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    windows: list[ExperimentWindow] = Field(min_length=1)
    repetitions: int = Field(default=1, ge=1, le=20)
    warmup_seconds: int = Field(default=5, alias="warmupSeconds", ge=2, le=720)
    warmup_rate: float = Field(default=4, alias="warmupRate", gt=0)
    generator_vus: int = Field(default=64, alias="generatorVus", ge=1, le=512)
    max_arrival_lateness: float = Field(
        default=0.25, alias="maxArrivalLatenessSeconds", gt=0
    )
    campaign_protocol: ArtifactReference | None = Field(
        default=None, alias="campaignProtocol"
    )
    campaign_block: int | None = Field(default=None, alias="campaignBlock", ge=0)
    fail_load: bool = Field(default=False, alias="failLoad")

    @model_validator(mode="after")
    def paired_protocol(self) -> Self:
        """Require the immutable protocol reference for each block."""
        if (self.campaign_protocol is None) != (self.campaign_block is None):
            raise ValueError("campaignProtocol and campaignBlock are required together")
        if self.campaign_protocol is not None and self.repetitions != 1:
            raise ValueError("a frozen campaign scenario executes exactly one block")
        return self


class OneShotLogs(BaseModel):
    """Explicit per-node collection; backends are external unless owned is requested."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = False
    endpoint: str | None = None
    image: str = "fluent/fluent-bit:5.1.3"
    cpu: float = Field(default=0.1, gt=0, le=2)
    memory_mib: int = Field(default=64, alias="memoryMiB", ge=32, le=1024)
    buffer_mib: int = Field(default=256, alias="bufferMiB", ge=1, le=4096)
    campaign_id: str = Field(
        default="one-shot", alias="campaignId", pattern=r"^[A-Za-z0-9_-]+$"
    )
    environment: str = Field(default="local", pattern=r"^[A-Za-z0-9_-]+$")
    backend_owned: bool = Field(default=False, alias="backendOwned")
    backend_bind: str = Field(default="127.0.0.1", alias="backendBind")
    export_limit: int = Field(default=5000, alias="exportLimit", ge=1, le=5000)

    @model_validator(mode="after")
    def validate_endpoint(self) -> Self:
        """Reject ambiguous endpoints and unpinned collectors before provisioning."""
        if self.enabled:
            if not self.endpoint:
                raise ValueError("logging endpoint is required")
            endpoint = urlsplit(self.endpoint)
            _ = endpoint.port
            if (
                endpoint.scheme not in ("http", "https")
                or not endpoint.hostname
                or not endpoint.path.endswith("/loki/api/v1/push")
                or endpoint.query
                or endpoint.fragment
                or endpoint.username
            ):
                raise ValueError(
                    "endpoint must be an http(s) Loki push URL without credentials"
                )
            if endpoint.hostname in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("endpoint must be reachable from the VMs")
            if self.backend_owned:
                from ipaddress import IPv4Address

                try:
                    binding = IPv4Address(self.backend_bind)
                except ValueError as error:
                    raise ValueError(
                        "owned backend needs a private IPv4 backendBind"
                    ) from error
                if (
                    endpoint.scheme != "http"
                    or not binding.is_private
                    or binding.is_loopback
                    or binding.is_unspecified
                    or binding.is_link_local
                ):
                    raise ValueError(
                        "owned backend needs private backendBind and an http endpoint"
                    )
            image_leaf = self.image.rsplit("/", 1)[-1]
            if ":" not in image_leaf or image_leaf.rsplit(":", 1)[-1] in ("", "latest"):
                raise ValueError("collector image must be pinned")
        return self

    def fingerprint(self) -> dict[str, JsonValue] | None:
        """Keep volatile campaign/run identity out of calibration compatibility."""
        if not self.enabled:
            return None
        return {
            **self.model_dump(by_alias=True, exclude={"campaign_id"}),
            "format": "json",
            "levels": {"controlPlane": "WARN", "functions": "runtime-default"},
            "collectorConfigVersion": 1,
            "backendImages": ["grafana/loki:3.7.8", "grafana/grafana:13.2.3"],
            "backendBudget": {"cpu": 1.5, "memoryMiB": 768}
            if self.backend_owned
            else None,
        }


class OneShotObservability(BaseModel):
    """Keep optional observation outside SDK and optimizer configuration."""

    model_config = ConfigDict(extra="forbid")
    logs: OneShotLogs = Field(default_factory=OneShotLogs)
    vm_resource_overhead: ArtifactReference | None = Field(
        default=None, alias="vmResourceOverhead"
    )


class OperationalLimits(BaseModel):
    """Observation procedure limits, independent of experimental resources."""

    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, populate_by_name=True
    )
    lead_seconds: float = Field(default=90, alias="leadSeconds", gt=0)
    max_operational_fraction: float = Field(
        default=1, alias="maxOperationalFraction", gt=0, le=1
    )
    api_seconds: float = Field(default=180, alias="apiSeconds", gt=0)
    barrier_seconds: float = Field(default=60, alias="barrierSeconds", gt=0)
    cloud_init_seconds: float = Field(default=1800, alias="cloudInitSeconds", gt=0)
    service_start_seconds: float = Field(default=300, alias="serviceStartSeconds", gt=0)
    compose_seconds: float = Field(default=300, alias="composeSeconds", gt=0)
    worker_start_seconds: float = Field(default=120, alias="workerStartSeconds", gt=0)
    replica_start_seconds: float = Field(default=300, alias="replicaStartSeconds", gt=0)
    bootstrap_invocation_seconds: float = Field(
        default=120, alias="bootstrapInvocationSeconds", gt=0
    )
    collection_http_seconds: float = Field(
        default=10, alias="collectionHttpSeconds", gt=0
    )
    generator_drain_seconds: float = Field(
        default=180, alias="generatorDrainSeconds", gt=0
    )
    worker_join_seconds: float = Field(default=300, alias="workerJoinSeconds", gt=0)
    process_drain_seconds: float = Field(default=30, alias="processDrainSeconds", gt=0)
    process_exit_seconds: float = Field(default=30, alias="processExitSeconds", gt=0)
    event_pages: int = Field(default=1000, alias="eventPages", ge=1)
    schedule_setup_seconds: float = Field(
        default=90, alias="scheduleSetupSeconds", gt=0
    )


def operational_limit(settings: object | None, name: str, default: float) -> float:
    """Use frozen observation limits while preserving explicit legacy defaults."""
    limits = getattr(settings, "operational_limits", None)
    return getattr(limits, name) if limits is not None else default


class OneShotConfig(BaseModel):
    """Provider/purpose boundaries and prerequisites checked before provisioning."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    protocol_version: Literal[1, 2] = Field(default=1, alias="protocolVersion")
    provider: Literal["multipass"]
    purpose: Literal["workflow-validation", "scientific-experiment"]
    nodes: list[OneShotNode] = Field(min_length=3)
    host_cpu_placement: HostCpuPlacement | None = Field(
        default=None, alias="hostCpuPlacement"
    )
    functions: dict[str, OneShotFunction] = Field(min_length=1)
    strict_memory: bool = Field(default=False, alias="strictMemory")
    profile: ArtifactReference | None = None
    qualification: ArtifactReference | None = None
    runtime_distribution: ArtifactReference | None = Field(
        default=None, alias="runtimeDistribution"
    )
    flow_quantum: float = Field(default=1, alias="flowQuantum", gt=0)
    seed: int = 7
    neighbors: dict[str, list[str]] | None = None
    observability: OneShotObservability = Field(default_factory=OneShotObservability)
    calibration: CalibrationSettings = Field(default_factory=CalibrationSettings)
    timing: TimingSettings | None = None
    experiment: ExperimentSettings | None = None
    operational_limits: OperationalLimits | None = Field(
        default=None, alias="operationalLimits"
    )

    @model_validator(mode="after")
    def validate_topology(self) -> Self:
        """Prevent aliases of a node and require a distinct terminal cloud."""
        if len({node.id for node in self.nodes}) != len(self.nodes):
            raise ValueError("node identifiers must be unique")
        if self.host_cpu_placement is not None:
            placement = self.host_cpu_placement
            if set(placement.nodes) != {node.id for node in self.nodes}:
                raise ValueError("host CPU placement node coverage must be exact")
            if any(
                len(placement.nodes[node.id].cpus) != node.cpus for node in self.nodes
            ):
                raise ValueError("host CPU placement must match VM vCPU count")
            if (
                self.observability.logs.enabled
                and self.observability.logs.backend_owned
                and len(placement.logging_cpus) < 2
            ):
                raise ValueError("owned logging CPU reservation requires two host CPUs")
        if sum(node.kind == "cloud" for node in self.nodes) != 1:
            raise ValueError("one terminal cloud is required")
        if sum(node.kind == "edge" for node in self.nodes) < 2:
            raise ValueError("at least two independent edges are required")
        if any(node.memory_capacity_mib >= node.memory_mib for node in self.nodes):
            raise ValueError("replica memory must leave room for the node runtime")
        for node in self.nodes:
            if node.function_limits.keys() - self.functions.keys():
                raise ValueError("unknown function override")
            functions = resolve_node_functions(self, node.id)
            if (
                self.strict_memory
                and sum(fn.memory_mib for fn in functions.values())
                > node.memory_capacity_mib
            ):
                raise ValueError(
                    "initial function replicas must fit simultaneously in memory pool"
                )
            if (
                self.strict_memory
                and self.purpose == "scientific-experiment"
                and node.memory_capacity_mib + 512 + 128 >= node.memory_mib
            ):
                raise ValueError(
                    "platform/OS memory reservation exceeds node memory budget"
                )
            for function in functions.values():
                if node.memory_capacity_mib < function.memory_mib:
                    raise ValueError("optimizer memory pool must fit one replica")
                if (
                    node.memory_capacity_mib // function.memory_mib
                    > function.max_replicas
                ):
                    raise ValueError("replica cap does not cover optimizer memory pool")
        if self.observability.logs.enabled:
            collector = self.observability.logs
            for node in self.nodes:
                reserved_memory = (
                    640
                    if self.strict_memory and self.purpose == "scientific-experiment"
                    else 0
                )
                if (
                    node.memory_capacity_mib + reserved_memory + collector.memory_mib
                    >= node.memory_mib
                ):
                    raise ValueError(
                        "collector memory reservation leaves no platform memory"
                    )
        return self


def resolve_node_functions(
    settings: OneShotConfig, node_id: str
) -> dict[str, OneShotFunction]:
    """Resolve only explicit overrides, without mutating shared settings."""
    node = next((node for node in settings.nodes if node.id == node_id), None)
    if node is None:
        raise ValueError(f"unknown node: {node_id}")
    return {
        name: function.model_copy(update=node.function_limits[name].model_dump())
        if name in node.function_limits
        else function.model_copy(deep=True)
        for name, function in settings.functions.items()
    }
