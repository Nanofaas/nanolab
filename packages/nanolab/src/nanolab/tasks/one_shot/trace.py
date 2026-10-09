"""One immutable relative trace produces oracle entries and original request IDs."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash


class TraceArtifact(BaseModel):
    """Complete quantized rates and payloads, independent of run time or endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: int = 1
    nodes: list[str]
    functions: list[str]
    period_seconds: float
    flow_quantum: float
    seed: int
    payloads: dict[str, dict[str, JsonValue]]
    windows: list[dict[str, dict[str, float]]]

    @model_validator(mode="after")
    def checked(self) -> Self:
        """Reject unrepresentable rates/counts and missing nodes or functions."""
        if self.period_seconds <= 0 or self.flow_quantum <= 0 or not self.windows:
            raise ValueError("trace requires positive period, quantum and windows")
        if (
            len(set(self.nodes)) != len(self.nodes)
            or len(set(self.functions)) != len(self.functions)
            or set(self.payloads) != set(self.functions)
        ):
            raise ValueError("trace scope mismatch")
        for window in self.windows:
            if set(window) != set(self.nodes):
                raise ValueError("trace node scope mismatch")
            for rates in window.values():
                if set(rates) != set(self.functions):
                    raise ValueError("trace function scope mismatch")
                for rate in rates.values():
                    if (
                        not math.isfinite(rate)
                        or rate < 0
                        or abs(
                            rate / self.flow_quantum - round(rate / self.flow_quantum)
                        )
                        > 1e-9
                        or abs(
                            rate * self.period_seconds
                            - round(rate * self.period_seconds)
                        )
                        > 1e-9
                    ):
                        raise ValueError("oracle rate/count is not representable")
        if not any(
            rate
            for window in self.windows
            for rates in window.values()
            for rate in rates.values()
        ):
            raise ValueError("trace must contain nonzero load")
        return self

    @property
    def sha256(self) -> str:
        """Hash canonical relative trace bytes, identical between modes."""
        return content_hash(canonical_bytes(self.model_dump()))


def oracle_entries(
    trace: TraceArtifact, *, node: str, generations: dict[str, int], anchor: datetime
) -> list[dict]:
    """Use the exact same intervals and rates as the request schedule."""
    return [
        {
            "function": name,
            "generation": generations[name],
            "start": (
                anchor + timedelta(seconds=epoch * trace.period_seconds)
            ).isoformat(),
            "end": (
                anchor + timedelta(seconds=(epoch + 1) * trace.period_seconds)
            ).isoformat(),
            "rate": rate,
            "unit": "requests/s",
        }
        for epoch, window in enumerate(trace.windows)
        for name, rate in window[node].items()
    ]


def request_schedule(
    trace: TraceArtifact, *, anchor: datetime, endpoints: dict[str, str], run_id: str
) -> list[dict]:
    """Assign every original exactly one evenly spaced emission, without retries."""
    rows = []
    for epoch, window in enumerate(trace.windows):
        for node, rates in window.items():
            for name, rate in rates.items():
                rows.extend(
                    {
                        "originalId": f"{run_id}:{epoch}:{node}:{name}:{index}",
                        "origin": node,
                        "function": name,
                        "epoch": epoch,
                        "phase": "campaign",
                        "scheduledAt": anchor.timestamp()
                        + epoch * trace.period_seconds
                        + (index + 0.5) / rate,
                        "url": endpoints[node] + f"/v1/functions/{name}:invoke",
                        "input": trace.payloads[name],
                    }
                    for index in range(round(rate * trace.period_seconds))
                )
    return sorted(rows, key=lambda row: (row["scheduledAt"], row["originalId"]))
