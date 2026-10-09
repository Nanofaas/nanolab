"""Versioned artifacts with explicit environment and purpose boundaries."""

from __future__ import annotations

from typing import Any, Literal, Self

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

    model_config = ConfigDict(extra="forbid")
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
    samples_seconds: list[float]

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
