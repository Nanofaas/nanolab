"""One immutable relative trace produces oracle entries and original request IDs."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from nanolab.config.one_shot import OneShotConfig
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

    node_roles: dict[str, Literal["edge", "cloud-direct"]] | None = None
    phase_ids: list[str] | None = None
    requested_windows: list[dict[str, dict[str, float]]] | None = None

    @model_serializer(mode="wrap")
    def serialized(self, handler: SerializerFunctionWrapHandler) -> dict:
        """Preserve exact legacy bytes when scientific metadata is absent."""
        result = handler(self)
        for key in ("node_roles", "phase_ids", "requested_windows"):
            if result.get(key) is None:
                result.pop(key, None)
        return result

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
        if self.node_roles is not None and set(self.node_roles) != set(self.nodes):
            raise ValueError("trace role scope mismatch")
        if self.phase_ids is not None and (
            len(self.phase_ids) != len(self.windows) or not all(self.phase_ids)
        ):
            raise ValueError("trace phase scope mismatch")
        if self.requested_windows is not None:
            if len(self.requested_windows) != len(self.windows):
                raise ValueError("requested trace scope mismatch")
            for window in self.requested_windows:
                if set(window) != set(self.nodes) or any(
                    set(rates) != set(self.functions)
                    or any(not math.isfinite(r) or r <= 0 for r in rates.values())
                    for rates in window.values()
                ):
                    raise ValueError("requested trace scope mismatch")
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
    if trace.node_roles and trace.node_roles[node] == "cloud-direct":
        return []
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
                        **(
                            {"originRole": trace.node_roles[node]}
                            if trace.node_roles
                            else {}
                        ),
                        **(
                            {"phaseId": trace.phase_ids[epoch]}
                            if trace.phase_ids
                            else {}
                        ),
                        "scheduledAt": anchor.timestamp()
                        + epoch * trace.period_seconds
                        + (index + 0.5) / rate,
                        "url": endpoints[node] + f"/v1/functions/{name}:invoke",
                        "input": trace.payloads[name],
                    }
                    for index in range(round(rate * trace.period_seconds))
                )
    return sorted(rows, key=lambda row: (row["scheduledAt"], row["originalId"]))


def _quantized(rate: float, quantum: float, *, upward: bool) -> float:
    count = (
        math.ceil(rate / quantum - 1e-9)
        if upward
        else math.floor(rate / quantum + 1e-9)
    )
    value = count * quantum
    if value <= 0:
        raise ValueError("quantization loses positive node traffic")
    return value


def validate_cloud_headroom(
    trace: TraceArtifact, *, cloud: str, empirical: float, physical: float | None = None
) -> None:
    """Budget every possible arrival using the smaller validated cloud capacity."""
    capacity = min(empirical, physical) if physical is not None else empirical
    if (
        not math.isfinite(capacity)
        or capacity <= 0
        or any(
            sum(sum(rates.values()) for rates in window.values())
            > 0.9 * capacity + 1e-9
            for window in trace.windows
        )
    ):
        raise ValueError("terminal cloud lacks declared ten percent physical headroom")


def capacity_trace(
    capacities: dict[str, float], settings: OneShotConfig, period: float, seed: int
) -> TraceArtifact:
    """Nine six-epoch phases, derived only from measured effective capacities."""
    nodes = [node.id for node in settings.nodes]
    edges = sorted(node.id for node in settings.nodes if node.kind == "edge")
    cloud = next(node.id for node in settings.nodes if node.kind == "cloud")
    if len(edges) != 5 or not {"edge-1", "edge-2"} <= set(edges):
        raise ValueError("scientific campaign requires its five declared edges")
    if (
        len(settings.functions) != 1
        or set(capacities) != set(nodes)
        or any(not math.isfinite(value) or value <= 0 for value in capacities.values())
    ):
        raise ValueError("measured capacity scope mismatch")
    name, function = next(iter(settings.functions.items()))
    hotspots = edges.copy()
    random.Random(seed).shuffle(hotspots)  # nosec B311: frozen experimental order
    phases = [("control", set(), 0.35)]
    phases += [("hotspot-" + edge, {edge}, 0.35) for edge in hotspots]
    phases += [
        ("adjacent", {"edge-1", "edge-2"}, 0.35),
        ("diffuse", set(edges), 1.10),
        ("recovery", set(), 0.35),
    ]
    windows, requested, phase_ids = [], [], []
    for phase, hot, background in phases:
        raw = {
            node: {
                name: capacities[node]
                * (
                    0.05
                    if node == cloud
                    else 1.10
                    if phase == "diffuse"
                    else 1.8
                    if node in hot
                    else background
                )
            }
            for node in nodes
        }
        effective = {
            node: {
                name: _quantized(
                    raw[node][name], settings.flow_quantum, upward=node in hot
                )
            }
            for node in nodes
        }
        windows.extend([effective] * 6)
        requested.extend([raw] * 6)
        phase_ids.extend([phase] * 6)
    trace = TraceArtifact(
        nodes=nodes,
        functions=[name],
        period_seconds=period,
        flow_quantum=settings.flow_quantum,
        seed=seed,
        payloads={name: function.input},
        windows=windows,
        node_roles={
            node: "cloud-direct" if node == cloud else "edge" for node in nodes
        },
        phase_ids=phase_ids,
        requested_windows=requested,
    )
    validate_cloud_headroom(trace, cloud=cloud, empirical=capacities[cloud])
    return trace


def experiment_trace(
    settings: OneShotConfig, *, period: float, aliases: dict[str, str]
) -> TraceArtifact:
    """Expand declared phase epochs once for both oracle and generator consumers."""
    if settings.experiment is None:
        raise ValueError("experiment windows are required")
    scientific = settings.purpose == "scientific-experiment"
    nodes = [n.id for n in settings.nodes if scientific or n.kind == "edge"]
    windows, phases = [], []
    for window in settings.experiment.windows:
        rates = {
            node: {aliases[key]: rate for key, rate in values.items()}
            for node, values in window.rates.items()
        }
        windows.extend([rates] * window.epochs)
        phases.extend([window.id] * window.epochs)
    return TraceArtifact(
        nodes=nodes,
        functions=list(aliases.values()),
        period_seconds=period,
        flow_quantum=settings.flow_quantum,
        seed=settings.seed,
        payloads={aliases[key]: fn.input for key, fn in settings.functions.items()},
        windows=windows,
        node_roles={
            n.id: "edge" if n.kind == "edge" else "cloud-direct" for n in settings.nodes
        }
        if scientific
        else None,
        phase_ids=phases if scientific else None,
    )


def warmup_trace(
    trace: TraceArtifact,
    *,
    seconds: float,
    capacities: dict[str, float] | None = None,
    legacy_rate: float = 4,
) -> TraceArtifact:
    """Use positive local capacity backgrounds, with a separate warmup interval."""
    if capacities is not None:
        if seconds < 2 * trace.period_seconds:
            raise ValueError("scientific warmup requires at least two periods")
        if set(capacities) != set(trace.nodes) or len(trace.functions) != 1:
            raise ValueError("warmup capacity scope mismatch")
        rates = {
            node: dict.fromkeys(
                trace.functions,
                _quantized(
                    capacities[node]
                    * (
                        0.05
                        if trace.node_roles and trace.node_roles[node] == "cloud-direct"
                        else 0.35
                    ),
                    trace.flow_quantum,
                    upward=False,
                ),
            )
            for node in trace.nodes
        }
    else:
        rates = {
            node: dict.fromkeys(trace.functions, legacy_rate) for node in trace.nodes
        }
    return TraceArtifact(
        nodes=trace.nodes,
        functions=trace.functions,
        period_seconds=seconds,
        flow_quantum=trace.flow_quantum,
        seed=trace.seed,
        payloads=trace.payloads,
        windows=[rates],
        node_roles=trace.node_roles,
        phase_ids=["warmup"] if trace.phase_ids else None,
    )
