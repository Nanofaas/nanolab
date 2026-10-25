"""Versioned artifacts with explicit environment and purpose boundaries."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from nanolab.one_shot.contracts import validate_contract

Purpose = Literal["workflow-validation", "scientific-experiment"]


class CalibrationProfile(BaseModel):
    """The NanoFaaS service profile, checked against its frozen v1 schema."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = Field(alias="schemaVersion")
    profile_id: str = Field(alias="profileId")
    provider: str
    purpose: Purpose
    synthetic: bool
    source_commit: str = Field(alias="sourceCommit")
    environment_fingerprint: str = Field(alias="environmentFingerprint")
    functions: list[dict[str, JsonValue]]
    environment: dict[str, str]

    @model_validator(mode="before")
    @classmethod
    def validate_schema(cls, data: Any) -> Any:
        """Retain all nested schema rules rather than reimplementing them."""
        validate_contract("service-profile", data)
        return data


def verify_calibration(
    profile: CalibrationProfile, *, provider: str, fingerprint: str, purpose: str
) -> None:
    """Forbid synthetic data or movement between target environments."""
    if profile.synthetic:
        raise ValueError("synthetic profile cannot qualify a measured workflow")
    if (
        profile.provider != provider
        or profile.environment_fingerprint != fingerprint
        or profile.purpose != purpose
    ):
        raise ValueError("calibration provider, fingerprint or purpose mismatch")


class TimingQualification(BaseModel):
    """Measured wall time and explicit period/lead-time selection."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    protocol_version: Literal[1, 2] = 1
    provider: str
    purpose: Purpose
    environment_fingerprint: str
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualified: bool
    period_seconds: float = Field(gt=0)
    lead_seconds: float = Field(gt=0)
    quantile: float = Field(gt=0, le=1)
    quantile_seconds: float = Field(ge=0)
    margin_seconds: float = Field(ge=0)
    epsilon: float = Field(gt=0, lt=1)
    sample_count: int = Field(gt=0)
    censored_count: int = Field(ge=0)
    samples_seconds: list[Annotated[float, Field(ge=0)] | None]
    minimum_samples: int = Field(default=1, ge=1)
    period_candidates: list[float] = Field(default_factory=list)
    max_trace_resolution: float | None = None
    ready_samples_seconds: list[Annotated[float, Field(ge=0)] | None] = Field(
        default_factory=list
    )
    protocol: dict[str, JsonValue] = Field(default_factory=dict)
    matrix: list[dict[str, JsonValue]] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_budget(self) -> Self:
        """Verify the qualification satisfies its declared time budget."""
        if self.qualified and (
            self.quantile_seconds + self.margin_seconds
            > self.epsilon * self.period_seconds
        ):
            raise ValueError("qualified period exceeds the declared auction budget")
        if len(self.samples_seconds) != self.sample_count:
            raise ValueError("timing sample count mismatch")
        if self.censored_count > self.sample_count:
            raise ValueError("timing censor count mismatch")
        if self.qualified and (
            self.censored_count
            or self.sample_count < self.minimum_samples
            or self.lead_seconds >= self.period_seconds
            or any(value is None for value in self.samples_seconds)
        ):
            raise ValueError("qualified timing has insufficient or censored evidence")
        if self.qualified:
            from nanolab.tasks.one_shot.statistics import quantile

            if self.quantile == 1 or self.sample_count < math.ceil(
                1 / (1 - self.quantile) - 1e-9
            ):
                raise ValueError("too few samples for the declared tail quantile")
            observed = [value for value in self.samples_seconds if value is not None]
            if self.quantile_seconds + 1e-9 < quantile(observed, self.quantile):
                raise ValueError("qualification understates measured auction wall time")
            if self.ready_samples_seconds:
                if len(self.ready_samples_seconds) != self.sample_count or any(
                    value is None for value in self.ready_samples_seconds
                ):
                    raise ValueError("qualified readiness has insufficient evidence")
                ready = [
                    value for value in self.ready_samples_seconds if value is not None
                ]
                if self.lead_seconds + 1e-9 < quantile(ready, self.quantile):
                    raise ValueError(
                        "qualification understates measured readiness time"
                    )
        return self


class CampaignManifest(BaseModel):
    """Frozen identities and protocol parameters of one comparison."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    provider: str
    purpose: Purpose
    environment_fingerprint: str
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int
    nodes: list[str] = Field(min_length=3)
    functions: list[str] = Field(min_length=1)
    period_seconds: float = Field(gt=0)
    lead_seconds: float = Field(gt=0)
    flow_quantum: float = Field(gt=0)
    modes: list[Literal["baseline", "oracle", "ewma", "holt", "ari"]]
    run_id: str = ""
    mode: Literal["baseline", "oracle", "ewma", "holt", "ari"] = "baseline"
    repetition: int = Field(default=0, ge=0)
    anchor: str = ""
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class CampaignBlock(BaseModel):
    """One registered seed and the exact three-mode execution order."""

    model_config = ConfigDict(extra="forbid", strict=True)
    block: int = Field(ge=0)
    seed: int
    modes: list[Literal["baseline", "oracle", "ewma"]]

    @model_validator(mode="after")
    def checked(self) -> Self:
        """Validate the frozen execution registry."""
        if len(self.modes) != 3 or set(self.modes) != {"baseline", "oracle", "ewma"}:
            raise ValueError("frozen block requires exactly the three modes")
        return self


class CampaignProtocol(BaseModel):
    """NanoLab protocol binds source settings, trace, order and planned replication."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1, 2] = Field(alias="schemaVersion")
    artifact_type: Literal["paired-one-shot-campaign"] = Field(alias="artifactType")
    nanolab_package_sha256: str = Field(
        alias="nanolabPackageSha256", pattern=r"^[0-9a-f]{64}$"
    )
    role: Literal["pilot", "main"]
    seed: int
    planned_blocks: int = Field(alias="plannedBlocks", ge=6, le=36)
    blocks: list[CampaignBlock]
    replacements: list[CampaignBlock]
    settings: dict[str, JsonValue]
    trace: dict[str, JsonValue]
    trace_sha256: str = Field(alias="traceSha256", pattern=r"^[0-9a-f]{64}$")
    profile_sha256: str = Field(alias="profileSha256", pattern=r"^[0-9a-f]{64}$")
    qualification_sha256: str = Field(
        alias="qualificationSha256", pattern=r"^[0-9a-f]{64}$"
    )
    environment_fingerprint: str = Field(
        alias="environmentFingerprint", pattern=r"^sha256:[0-9a-f]{64}$"
    )
    pilot: dict[str, JsonValue] | None
    analysis: dict[str, JsonValue]

    @model_validator(mode="after")
    def checked(self) -> Self:
        """Validate the frozen execution registry."""
        from collections import Counter

        from nanolab.tasks.one_shot.trace import TraceArtifact

        n = self.planned_blocks
        replacement_count = math.floor(0.20 * n) if self.role == "main" else 0
        if (
            (self.role == "pilot" and n != 6)
            or (self.role == "main" and (n < 12 or n % 6))
            or len(self.blocks) != n
            or {b.block for b in self.blocks} != set(range(n))
            or len(self.replacements) != replacement_count
            or {b.block for b in self.replacements}
            != set(range(n, n + replacement_count))
        ):
            raise ValueError("frozen campaign registry is incomplete or outside budget")
        order = Counter(tuple(b.modes) for b in self.blocks)
        if len(order) != 6 or set(order.values()) != {n // 6}:
            raise ValueError("frozen mode order is not balanced")
        seeds = [b.seed for b in self.blocks + self.replacements]
        if len(seeds) != len(set(seeds)):
            raise ValueError("frozen campaign seeds must be unique")
        trace = TraceArtifact.model_validate(self.trace)
        if trace.sha256 != self.trace_sha256:
            raise ValueError("frozen trace hash mismatch")
        if self.schema_version == 2 or trace.schema_version == 2:
            from nanolab.one_shot.campaign import analysis_denominator, analysis_epochs
            from nanolab.one_shot.vm_resources import telemetry_policy

            if self.analysis.get("vmTelemetry") != telemetry_policy():
                raise ValueError("frozen VM telemetry policy/asset differs")
            aliases = self.analysis.get("functionAliases")
            if (
                self.schema_version != 2
                or trace.schema_version != 2
                or self.analysis.get("analysisEpochIndices") != analysis_epochs(trace)
                or self.analysis.get("denominator") != analysis_denominator(trace)
                or not isinstance(aliases, dict)
                or set(aliases) != set(cast(dict, self.settings["functions"]))
                or not all(isinstance(value, str) for value in aliases.values())
                or aliases
                != {
                    key: key if key in trace.functions else key.removesuffix("-rust")
                    for key in cast(dict, self.settings["functions"])
                }
                or len(set(aliases.values())) != 5
                or set(aliases.values()) != set(trace.functions)
            ):
                raise ValueError("synchronized campaign analysis/alias scope mismatch")
        return self
