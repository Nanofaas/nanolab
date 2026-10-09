"""Versioned artifacts with explicit environment and purpose boundaries."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Self

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
    modes: list[Literal["baseline", "oracle", "ewma"]]
    run_id: str = ""
    mode: Literal["baseline", "oracle", "ewma"] = "baseline"
    repetition: int = Field(default=0, ge=0)
    anchor: str = ""
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
