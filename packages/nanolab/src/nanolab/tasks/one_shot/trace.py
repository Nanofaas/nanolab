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


def concurrent_trace(
    records: list[dict],
    settings: OneShotConfig,
    *,
    period: float,
    seed: int,
    aliases: dict[str, str],
) -> TraceArtifact:
    """Build from complete concurrent records; qualify cloud mixtures separately."""
    from nanolab.one_shot.capacity import capacity_configurations

    edges = [f"edge-{number}" for number in range(1, 6)]
    graph = {
        edges[0]: {edges[1], edges[2]},
        edges[1]: {edges[0], edges[2]},
        edges[2]: {edges[0], edges[1], edges[3], edges[4]},
        edges[3]: {edges[2], edges[4]},
        edges[4]: {edges[2], edges[3]},
    }
    if (
        {node.id for node in settings.nodes} != {*edges, "cloud"}
        or settings.neighbors is None
        or {node: set(peers) for node, peers in settings.neighbors.items()} != graph
    ):
        raise ValueError("concurrent trace requires the two triangles scope")
    if (
        not settings.strict_memory
        or len(settings.functions) != 5
        or set(aliases) != set(settings.functions)
        or len(set(aliases.values())) != 5
        or not isinstance(records, list)
    ):
        raise ValueError("concurrent capacity/function scope mismatch")
    if period != 360:
        raise ValueError(
            "synchronized campaign requires period360; "
            "requalify other periods separately"
        )
    names, nodes = list(aliases.values()), [node.id for node in settings.nodes]
    balanced = next(
        entry["vectors"]
        for entry in capacity_configurations(settings, aliases)
        if entry["kind"] == "mixed" and entry["dominant"] is None
    )
    capacities = {}
    for node, vector in balanced.items():
        candidates = [
            entry
            for entry in records
            if entry.get("node") == node
            and entry.get("replicaVector") == vector
            and isinstance(entry.get("rates"), dict)
            and set(entry["rates"]) == set(names)
            and all(
                type(rate) in (int, float) and math.isfinite(rate) and rate > 0
                for rate in entry["rates"].values()
            )
        ]
        if not candidates:
            raise ValueError("missing qualified balanced concurrent capacity scope")
        # Select ONE measured vector, never assemble per-function maxima.
        capacities[node] = max(
            candidates, key=lambda entry: sum(entry["rates"].values())
        )
    hotspots = edges.copy()
    random.Random(seed).shuffle(hotspots)  # nosec B311: frozen experimental order
    conditions = [("control", set())]
    conditions += [("hotspot-" + edge, {edge}) for edge in hotspots]
    conditions += [
        ("two-triangles", {"edge-2", "edge-4"}),
        ("diffuse-subset", {"edge-1", "edge-3", "edge-5"}),
        ("recovery", set()),
    ]
    windows, requested = [], []
    for epoch, (phase, hot) in enumerate(conditions):
        raw, effective = {}, {}
        for position, node in enumerate(nodes):
            raw[node], effective[node] = {}, {}
            for name in names:
                overloaded = node in hot and (
                    phase == "diffuse-subset" or name == names[edges.index(node)]
                )
                factor = (
                    0.035 + epoch * 0.001
                    if node == "cloud"
                    else 1.25
                    if phase == "diffuse-subset" and node in hot
                    else 1.8
                    if overloaded
                    else 0.29 + position * 0.012 + epoch * 0.006
                )
                rate = capacities[node]["rates"][name]
                raw[node][name] = rate * factor
                effective[node][name] = _quantized(
                    raw[node][name], settings.flow_quantum, upward=overloaded
                )
                if overloaded and effective[node][name] <= rate:
                    raise ValueError("quantization loses intended capacity overload")
        requested.append(raw)
        windows.append(effective)
    trace = TraceArtifact(
        nodes=nodes,
        functions=names,
        period_seconds=period,
        flow_quantum=settings.flow_quantum,
        seed=seed,
        payloads={aliases[key]: fn.input for key, fn in settings.functions.items()},
        windows=windows,
        requested_windows=requested,
        node_roles={
            node: "cloud-direct" if node == "cloud" else "edge" for node in nodes
        },
        phase_ids=[phase for phase, _hot in conditions],
    )
    validate_multi_cloud_bounds(
        trace, cloud="cloud", rates=capacities["cloud"]["rates"]
    )
    return trace


def cloud_capacity_requirements(
    trace: TraceArtifact, cloud: str
) -> list[dict[str, float]]:
    """Predeclare the exact joint SDK rates that prove a ten-percent cloud margin."""
    if cloud not in trace.nodes:
        raise ValueError("cloud capacity scope mismatch")
    return [
        {
            name: math.ceil(
                sum(window[node][name] for node in trace.nodes) / 0.9 * 90 - 1e-9
            )
            / 90
            for name in trace.functions
        }
        for window in trace.windows
    ]


def validate_multi_cloud_bounds(
    trace: TraceArtifact,
    *,
    cloud: str,
    rates: dict[str, float],
    physical: dict[str, float] | None = None,
) -> None:
    """Screen each function, including cloud-direct traffic and possible offload."""
    if set(rates) != set(trace.functions) or (
        physical is not None and set(physical) != set(rates)
    ):
        raise ValueError("cloud function capacity scope mismatch")
    for name in trace.functions:
        capacity = (
            min(rates[name], physical[name]) if physical is not None else rates[name]
        )
        if (
            type(capacity) not in (int, float)
            or not math.isfinite(capacity)
            or capacity <= 0
            or any(
                sum(window[node][name] for node in trace.nodes) > 0.9 * capacity + 1e-9
                for window in trace.windows
            )
        ):
            raise ValueError("terminal cloud function lacks ten percent headroom")


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
    if (
        len(trace.functions) == 5
        and len(trace.windows) == 9
        and trace.period_seconds == 360
    ):
        if seconds < 2 * trace.period_seconds:
            raise ValueError("scientific warmup requires at least two periods")
        rates = trace.windows[0]
    elif capacities is not None:
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
