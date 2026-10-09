"""Strict configuration of the independent local one-shot workflows."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Literal, Self

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


class OneShotNode(BaseModel):
    """A separate Multipass VM for one logical edge or terminal cloud."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    kind: Literal["edge", "cloud"]
    cpus: int = Field(default=2, gt=0)
    memory_mib: int = Field(default=4096, alias="memoryMiB", gt=0)
    disk_gib: int = Field(default=20, alias="diskGiB", gt=0)
    memory_capacity_mib: int = Field(default=256, alias="memoryCapacityMiB", gt=0)


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


class ExperimentSettings(BaseModel):
    """Explicit comparison windows, repetitions and generator capacity."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    windows: list[TimingCell] = Field(min_length=1)
    repetitions: int = Field(default=1, ge=1, le=20)
    warmup_seconds: int = Field(default=5, alias="warmupSeconds", ge=2, le=60)
    warmup_rate: float = Field(default=4, alias="warmupRate", gt=0)
    generator_vus: int = Field(default=64, alias="generatorVus", ge=1, le=512)
    max_arrival_lateness: float = Field(
        default=0.25, alias="maxArrivalLatenessSeconds", gt=0
    )
    fail_load: bool = Field(default=False, alias="failLoad")


class OneShotConfig(BaseModel):
    """Provider/purpose boundaries and prerequisites checked before provisioning."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    provider: Literal["multipass"]
    purpose: Literal["workflow-validation"]
    nodes: list[OneShotNode] = Field(min_length=3)
    functions: dict[str, OneShotFunction] = Field(min_length=1)
    profile: ArtifactReference | None = None
    qualification: ArtifactReference | None = None
    runtime_distribution: ArtifactReference | None = Field(
        default=None, alias="runtimeDistribution"
    )
    flow_quantum: float = Field(default=1, alias="flowQuantum", gt=0)
    seed: int = 7
    calibration: CalibrationSettings = Field(default_factory=CalibrationSettings)
    timing: TimingSettings | None = None
    experiment: ExperimentSettings | None = None

    @model_validator(mode="after")
    def validate_topology(self) -> Self:
        """Prevent aliases of a node and require a distinct terminal cloud."""
        if len({node.id for node in self.nodes}) != len(self.nodes):
            raise ValueError("node identifiers must be unique")
        if sum(node.kind == "cloud" for node in self.nodes) != 1:
            raise ValueError("one terminal cloud is required")
        if sum(node.kind == "edge" for node in self.nodes) < 2:
            raise ValueError("at least two independent edges are required")
        if any(node.memory_capacity_mib >= node.memory_mib for node in self.nodes):
            raise ValueError("replica memory must leave room for the node runtime")
        for node in self.nodes:
            for function in self.functions.values():
                if node.memory_capacity_mib < function.memory_mib:
                    raise ValueError("optimizer memory pool must fit one replica")
                if (
                    node.memory_capacity_mib // function.memory_mib
                    > function.max_replicas
                ):
                    raise ValueError("replica cap does not cover optimizer memory pool")
        return self
