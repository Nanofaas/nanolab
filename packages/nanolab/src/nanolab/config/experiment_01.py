"""The protocol of experiment 01: control-plane cost in front of one function.

Only the parameters of the protocol live here. Which builds are compared is the
recipe catalogue's business (`comparison.profiles`), and the order the cells run
in is the matrix's; none of the values below is an axis of that matrix, so every
cell of a run sends the same payload on the same calendar.

The calendar is idle, warmup, then plateaux at constant arrival rate with sharp
changes, then a bounded drain. Each plateau stops *starting* requests at its end,
but requests already sent may finish during the next one: `gracefulStopSeconds`
is that allowance, and it must cover the HTTP timeout plus a margin, or k6 would
cancel at the boundary exactly the slow responses the experiment exists to see.

A frozen protocol is the one a campaign runs. A pilot may leave the generator
fidelity thresholds open, because choosing them is part of what the pilot is for;
a frozen one may not, because a step whose load was not delivered must be
recognisable as such against a threshold fixed before the run.
"""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveInt = Annotated[int, Field(gt=0)]
PositiveNumber = Annotated[float, Field(gt=0)]
NonNegativeNumber = Annotated[float, Field(ge=0)]
Fraction = Annotated[float, Field(ge=0, lt=1)]

EXPERIMENT_01_FUNCTION = "qr-code-rust"
# The heap ceiling every runtime of the control plane is held to. Not a
# protocol field on purpose: raising it to hide an OOM is what the experiment
# forbids, so it cannot be one value in a scenario file.
CONTROL_PLANE_MAX_HEAP_MIB = 64


class Phase(TypedDict):
    """One scheduled phase, offset from the calendar origin."""

    name: str
    rate: float
    startSeconds: int
    durationSeconds: int


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, populate_by_name=True
    )


class Plateau(_StrictModel):
    """One constant-arrival-rate step of the calendar."""

    rate: PositiveInt
    duration_seconds: PositiveInt = Field(alias="durationSeconds")


class GeneratorFidelity(_StrictModel):
    """When a step counts as delivered by the generator.

    Checked before any sustainability verdict: a step whose requested rate was
    not sent says nothing about the system at that rate, however well the
    requests that were sent were served.
    """

    max_rate_deviation: Fraction = Field(alias="maxRateDeviation")
    max_dropped_fraction: Fraction = Field(alias="maxDroppedFraction")
    window_seconds: PositiveInt = Field(alias="windowSeconds")
    max_start_delay_seconds: NonNegativeNumber = Field(alias="maxStartDelaySeconds")


class SustainabilityCriteria(_StrictModel):
    """The manually frozen thresholds a delivered step is judged against."""

    latency_threshold_ms: PositiveNumber = Field(alias="latencyThresholdMs")
    min_within_threshold_fraction: Annotated[float, Field(gt=0, le=1)] = Field(
        alias="minWithinThresholdFraction"
    )
    max_pending_fraction: Fraction = Field(alias="maxPendingFraction")


class Experiment01Protocol(_StrictModel):
    """Everything a cell of experiment 01 sends, and how it is judged."""

    status: Literal["pilot", "frozen"]
    payload_file: Annotated[str, Field(min_length=1)] = Field(alias="payloadFile")
    payload_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] = Field(
        alias="payloadSha256"
    )
    idle_seconds: PositiveInt = Field(alias="idleSeconds")
    warmup: Plateau
    steps: list[Plateau] = Field(min_length=1)
    http_timeout_seconds: PositiveInt = Field(alias="httpTimeoutSeconds")
    completion_margin_seconds: PositiveInt = Field(alias="completionMarginSeconds")
    graceful_stop_seconds: PositiveInt = Field(alias="gracefulStopSeconds")
    drain_seconds: PositiveInt = Field(alias="drainSeconds")
    fixed_concurrency: PositiveInt = Field(alias="fixedConcurrency")
    # Applied only to the composition that has a queue; the baseline has none
    # and admits however the core does, which the report records as it is.
    queue_capacity: PositiveInt = Field(alias="queueCapacity")
    pre_allocated_vus: PositiveInt = Field(alias="preAllocatedVUs")
    max_vus: PositiveInt = Field(alias="maxVUs")
    criteria: SustainabilityCriteria
    generator: GeneratorFidelity | None = None

    @model_validator(mode="after")
    def coherent_calendar(self) -> Self:
        """Reject a calendar k6 would execute differently from what it says."""
        rates = [step.rate for step in self.steps]
        if any(later <= earlier for earlier, later in pairwise(rates)):
            raise ValueError("experiment01 step rates must be strictly increasing")
        if self.payload_file != self.payload_file.strip() or "/" in self.payload_file:
            raise ValueError("experiment01 payloadFile is a payload basename")
        if (
            self.graceful_stop_seconds
            < self.http_timeout_seconds + self.completion_margin_seconds
        ):
            raise ValueError(
                "experiment01 gracefulStopSeconds must cover the HTTP timeout "
                "plus the completion margin"
            )
        if self.drain_seconds < self.graceful_stop_seconds:
            raise ValueError("experiment01 drainSeconds must cover gracefulStopSeconds")
        if self.pre_allocated_vus > self.max_vus:
            raise ValueError("experiment01 preAllocatedVUs cannot exceed maxVUs")
        if self.status == "frozen" and self.generator is None:
            raise ValueError(
                "a frozen experiment01 protocol requires explicit generator "
                "fidelity thresholds"
            )
        if self.generator is not None and self.generator.window_seconds > min(
            step.duration_seconds for step in (self.warmup, *self.steps)
        ):
            raise ValueError(
                "experiment01 generator windowSeconds cannot exceed a plateau"
            )
        return self

    def phases(self) -> list[Phase]:
        """Return the scheduled phases, with offsets from the calendar origin.

        The origin is the start of idle. Load phases start back to back; each
        keeps its own `gracefulStop`, so a response may land after the next one
        has begun and still belongs to the phase it was sent in.
        """
        phases: list[Phase] = [
            {
                "name": "idle",
                "rate": 0.0,
                "startSeconds": 0,
                "durationSeconds": self.idle_seconds,
            }
        ]
        offset = self.idle_seconds
        for index, step in enumerate((self.warmup, *self.steps)):
            phases.append(
                {
                    "name": "warmup" if index == 0 else f"step-{index:02d}",
                    "rate": step.rate,
                    "startSeconds": offset,
                    "durationSeconds": step.duration_seconds,
                }
            )
            offset += step.duration_seconds
        phases.append(
            {
                "name": "drain",
                "rate": 0.0,
                "startSeconds": offset,
                "durationSeconds": self.drain_seconds,
            }
        )
        return phases


def experiment_01_payload(protocol: Experiment01Protocol, payload_dir: Path) -> bytes:
    """Return the exact body every request sends, verified against its hash.

    The bytes are sent as they are on disk, not re-serialized: the hash in the
    protocol is the hash of what goes over the wire.
    """
    raw = (payload_dir / protocol.payload_file).read_bytes()
    if hashlib.sha256(raw).hexdigest() != protocol.payload_sha256:
        raise ValueError("experiment01 payload differs from its declared sha256")
    body = json.loads(raw)
    if (
        not isinstance(body, dict)
        or set(body) != {"text", "size"}
        or not isinstance(body["text"], str)
        or not body["text"]
        or len(body["text"].encode()) > 1024
        or type(body["size"]) is not int
        or not 128 <= body["size"] <= 1024
    ):
        raise ValueError("experiment01 payload is not a qr-code text/size request")
    return raw
