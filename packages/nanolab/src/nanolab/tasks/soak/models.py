"""Immutable values carried between soak observation and evaluation tasks."""

from dataclasses import dataclass
from typing import Literal

Status = Literal["PASS", "FAIL", "INCONCLUSIVE", "ABORTED"]
Availability = Literal["observed", "unavailable", "not_applicable"]
Phase = Literal["preflight", "warmup", "baseline", "steady", "drain", "diagnostic"]


@dataclass(frozen=True)
class Target:
    """Identify a process incarnation, not merely a reusable container name."""

    role: str
    container_id: str
    process_id: int
    process_started_at: str
    image_digest: str
    runtime: str


@dataclass(frozen=True)
class Sample:
    """Preserve labels, source, units, timing and availability for one observation."""

    target: Target
    phase: Phase
    scheduled_s: float
    started_s: float
    ended_s: float
    metric: str
    labels: tuple[tuple[str, str], ...]
    unit: str
    value: float | None
    availability: Availability
    source: str
    reason: str | None


@dataclass(frozen=True)
class CriterionResult:
    """Retain a criterion's evidence even when another criterion is inconclusive."""

    criterion_id: str
    status: Status
    reason: str
    evidence: tuple[str, ...]
