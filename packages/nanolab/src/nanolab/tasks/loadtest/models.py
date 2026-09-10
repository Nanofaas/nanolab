"""Data models shared by the load-test report and adapter layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sonata_engine import Resource
from sonata_tasks.k6_models import K6RunResult, K6Stage

__all__ = ["K6Config", "K6RunResult", "K6Stage", "PrometheusQuery", "TimeWindow"]


@dataclass(frozen=True)
class K6Config:
    """The script, target and load shape of a single k6 run."""

    script_path: Path
    target_url: str | Resource[str]
    summary_output_path: Path
    stages: tuple[K6Stage, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    vus: int | None = None
    duration: str | None = None
    payload_path: Path | None = None

    def __post_init__(self) -> None:
        """Normalise `stages` to a tuple and copy `env` for a frozen instance."""
        object.__setattr__(self, "stages", tuple(self.stages))
        object.__setattr__(self, "env", dict(self.env))


@dataclass(frozen=True)
class TimeWindow:
    """A `start`/`end` pair delimiting the period a query or report covers."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class PrometheusQuery:
    """A named PromQL expression and whether the report requires it."""

    name: str
    expr: str
    required: bool = False
