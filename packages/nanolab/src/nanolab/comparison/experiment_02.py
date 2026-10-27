"""Experiment 02 matrix data: configurations, cells, order and identity.

A cell of experiment 02 is one configuration, under one budget, in one
repetition. A configuration is a complete deployment: a nanoFaaS composition,
a baseline platform, or the function alone with no platform in front of it.
The budget is the deployment's CPU quota and memory limit, so the same
configuration under two budgets is two cells, not one cell measured twice.

The order is shuffled per repetition with a seed the protocol declares. A grid
of configurations and budgets is too large for hand-written orders like
experiment 01's, and running each configuration's budgets back to back would
make "which budget" and "when it ran" the same axis. The seed makes the
shuffle reproducible, which is what a resume needs.

Receipts, the ledger and the verdicts are experiment 01's. Only the evidence a
measured cell must leave behind differs: cgroup samples instead of a
Prometheus snapshot.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from nanolab.comparison.experiment_01 import (
    CELL_RESULT,
    EVENTS,
    SUMMARY,
    attempt_dir,
    build_ledger,
    identity_sha256,
    read_events,
    summarize_phases,
)
from nanolab.config.experiment_02 import Budget, Experiment02Protocol
from nanolab.tasks.experiment_02.budget import ENGINE_CGROUPS
from nanolab.tasks.experiment_02.samples import (
    CPU_WINDOW_SECONDS,
    DEPLOYMENT_CPU,
    DEPLOYMENT_MEMORY,
    SAMPLE_INTERVAL_SECONDS,
    as_snapshot,
    container_count,
    cpu_cores,
    cpu_seconds,
    memory_byte_seconds,
    read_cgroup_samples,
    working_set,
)

Platform = Literal["nanofaas", "direct"]

SAMPLES = "metrics/cgroup-samples.jsonl"
# nanoFaaS's raw Prometheus exposition, scraped beside the samples.
SCHEDULER = "metrics/scheduler-samples.jsonl"
EXPERIMENT_02_EVIDENCE: tuple[str, ...] = (SUMMARY, EVENTS, SAMPLES)

EXPERIMENT_02_ORDER_POLICY = "experiment-02-seeded-shuffle-v1"


@dataclass(frozen=True, slots=True)
class Configuration:
    """One complete deployment the experiment measures."""

    key: str
    label: str
    platform: Platform
    profile: Literal["P0", "P1"] | None = None
    runtime: Literal["jvm", "native"] | None = None
    # The recipe that builds it, for configurations prepare must publish.
    recipe: str | None = None


EXPERIMENT_02_CONFIGURATIONS: tuple[Configuration, ...] = (
    # First, because its recipe publishes the function every configuration uses.
    Configuration(
        "nanofaas-p1-jvm", "nanoFaaS P1, JVM", "nanofaas", "P1", "jvm",
        "experiment-02-p1-jvm",
    ),
    Configuration(
        "nanofaas-p1-native", "nanoFaaS P1, native", "nanofaas", "P1", "native",
        "experiment-02-p1-native",
    ),
    Configuration(
        "nanofaas-p0-jvm", "nanoFaaS P0, JVM", "nanofaas", "P0", "jvm",
        "experiment-02-p0-jvm",
    ),
    Configuration(
        "nanofaas-p0-native", "nanoFaaS P0, native", "nanofaas", "P0", "native",
        "experiment-02-p0-native",
    ),
    Configuration("direct-reference", "Function without a platform", "direct"),
)  # fmt: skip
CONFIGURATIONS_BY_KEY = {
    configuration.key: configuration for configuration in EXPERIMENT_02_CONFIGURATIONS
}
EXPERIMENT_02_PUBLISHER = EXPERIMENT_02_CONFIGURATIONS[0].key


def required_evidence(configuration: Configuration) -> tuple[str, ...]:
    """Return what a measured cell of `configuration` must leave behind."""
    if configuration.platform == "nanofaas":
        return (*EXPERIMENT_02_EVIDENCE, SCHEDULER)
    return EXPERIMENT_02_EVIDENCE


@dataclass(frozen=True, slots=True)
class Experiment02Cell:
    """One configuration, under one budget, in one repetition."""

    configuration: Configuration
    budget: Budget
    repetition: int

    @property
    def key(self) -> str:
        """Return the configuration this cell measures."""
        return self.configuration.key

    @property
    def label(self) -> str:
        """Return the name this cell takes in the manifest."""
        return f"{self.key} {self.budget.label} run {self.repetition}"

    def run_dir(self, root: Path) -> Path:
        """Return the directory this cell's run writes its artefacts to."""
        return root / self.key / self.budget.label / f"run-{self.repetition}"


def build_experiment_02_matrix(
    configurations: tuple[Configuration, ...],
    budgets: tuple[Budget, ...],
    repetitions: int,
    *,
    seed: int,
) -> tuple[Experiment02Cell, ...]:
    """Every (configuration, budget) pair once per repetition, shuffled per repetition.

    A string seed is hashed the same way on every run, so the order is
    reproducible from the protocol alone.
    """
    if repetitions < 1:
        raise ValueError("experiment-02 needs at least one repetition")
    if not configurations:
        raise ValueError("experiment-02 needs at least one configuration")
    if not budgets:
        raise ValueError("experiment-02 needs at least one budget")
    cells: list[Experiment02Cell] = []
    for repetition in range(1, repetitions + 1):
        pairs = [
            (configuration, budget)
            for configuration in configurations
            for budget in budgets
        ]
        order = random.Random(f"{seed}:{repetition}")  # nosec B311 - not a secret
        order.shuffle(pairs)
        cells.extend(
            Experiment02Cell(configuration, budget, repetition)
            for configuration, budget in pairs
        )
    return tuple(cells)


def experiment_02_identity(
    protocol: Experiment02Protocol,
    cells: tuple[Experiment02Cell, ...],
    *,
    payload: bytes,
    script: Path,
    selected: tuple[str, ...],
    seed: int,
) -> dict[str, object]:
    """Return what the manifest records about experiment 02 beyond the scenario."""
    values = protocol.model_dump(mode="json", by_alias=True)
    return {
        "profile": "experiment-02",
        "orderPolicy": EXPERIMENT_02_ORDER_POLICY,
        "seed": seed,
        "order": [cell.label for cell in cells],
        "protocol": values,
        "protocolSha256": identity_sha256(values),
        "payload": {
            "file": protocol.payload_file,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        },
        "script": {
            "name": script.name,
            "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        },
        "configurations": {
            key: {
                "label": CONFIGURATIONS_BY_KEY[key].label,
                "platform": CONFIGURATIONS_BY_KEY[key].platform,
                "profile": CONFIGURATIONS_BY_KEY[key].profile,
                "runtime": CONFIGURATIONS_BY_KEY[key].runtime,
                "recipe": CONFIGURATIONS_BY_KEY[key].recipe,
            }
            for key in selected
        },
        "budgets": [budget.label for budget in protocol.budgets],
        "subset": set(selected) != set(CONFIGURATIONS_BY_KEY),
    }


# ---------------------------------------------------------------- summaries


def _mean_inside(
    points: Sequence[tuple[float, float]], start: float, end: float
) -> float | None:
    inside = [value for at, value in points if start <= at < end]
    return statistics.fmean(inside) if inside else None


def _phase_costs(
    samples: Sequence[Mapping[str, Any]],
    row: Mapping[str, Any],
    *,
    start: float | None,
    end: float | None,
    load: bool,
) -> dict[str, object]:
    """Return a phase's engine share, live instances and cost per useful response.

    The cost covers the phase's own window. A cohort's late responses may land
    in the next phase, by at most `gracefulStopSeconds`; that overlap is the
    known limit of reading a step's cost from its window.
    """
    if start is None or end is None:
        names = [
            "engineWorkingSetMeanBytes",
            "engineCpuMeanCores",
            "containersMean",
            "containersMax",
        ]
        if load:
            names += [
                "cpuSeconds",
                "memoryByteSeconds",
                "cpuSecondsPerUseful",
                "memoryByteSecondsPerUseful",
            ]
        unknown: dict[str, object] = {}
        for name in names:
            unknown[name] = None
        return unknown
    counts = [count for at, count in container_count(samples) if start <= at < end]
    costs: dict[str, object] = {
        "engineWorkingSetMeanBytes": _mean_inside(
            working_set(samples, ENGINE_CGROUPS), start, end
        ),
        # A rate whose window reaches into the previous phase is left out.
        "engineCpuMeanCores": _mean_inside(
            cpu_cores(samples, ENGINE_CGROUPS), start + CPU_WINDOW_SECONDS, end
        ),
        "containersMean": statistics.fmean(counts) if counts else None,
        "containersMax": max(counts) if counts else None,
    }
    if load:
        useful = int(row["withinThreshold"])
        cpu = cpu_seconds(samples, start, end)
        memory = memory_byte_seconds(samples, start, end)
        costs |= {
            "cpuSeconds": cpu,
            "memoryByteSeconds": memory,
            # Undefined, not zero, when nothing useful came back.
            "cpuSecondsPerUseful": cpu / useful if useful and cpu is not None else None,
            "memoryByteSecondsPerUseful": (
                memory / useful if useful and memory is not None else None
            ),
        }
    return costs


def summarize_experiment_02_cell(
    cell: Experiment02Cell, root: Path, protocol: Experiment02Protocol
) -> dict[str, object]:
    """Summarise one cell from the attempt its receipt selects.

    Experiment 01's per-phase rows and verdicts, read from the deployment's
    cgroup, plus what experiment 02 adds: the engine's share, the live
    instances, and CPU time and memory occupancy per useful response.
    """
    cell_dir = cell.run_dir(root)
    receipt = json.loads((cell_dir / CELL_RESULT).read_text(encoding="utf-8"))
    selected = attempt_dir(cell_dir, int(receipt["attempt"]))
    readiness = selected / "readiness.json"
    result: dict[str, object] = {
        "cell": receipt["cell"],
        "configuration": cell.key,
        "budget": cell.budget.label,
        "repetition": cell.repetition,
        "attempt": receipt["attempt"],
        "outcome": receipt["outcome"],
        "reason": receipt["reason"],
        "notProduced": receipt["notProduced"],
        "readiness": json.loads(readiness.read_text()) if readiness.is_file() else None,
        "sampleIntervalSeconds": SAMPLE_INTERVAL_SECONDS,
        "cpuRateWindowSeconds": CPU_WINDOW_SECONDS,
    }
    if receipt["outcome"] != "measured":
        result["phases"] = []
        return result
    events, malformed = read_events(selected / EVENTS)
    ledger = build_ledger(events)
    samples = read_cgroup_samples(selected / SAMPLES)
    rows = summarize_phases(
        protocol,
        ledger,
        summary=json.loads((selected / SUMMARY).read_text(encoding="utf-8")),
        snapshot=as_snapshot(samples.samples),
        malformed_events=malformed,
        memory_series=DEPLOYMENT_MEMORY,
        cpu_series=DEPLOYMENT_CPU,
    )
    origin = ledger["origin"]
    for row, phase in zip(rows, protocol.phases(), strict=True):
        start = None if origin is None else origin / 1000 + phase["startSeconds"]
        end = None if start is None else start + phase["durationSeconds"]
        row.update(
            _phase_costs(
                samples.samples, row, start=start, end=end, load=phase["rate"] > 0
            )
        )
    result["malformedEvents"] = malformed
    result["malformedSamples"] = samples.malformed
    result["anomalies"] = ledger["anomalies"]
    result["phases"] = rows
    return result


_CSV_FIELDS: tuple[str, ...] = (
    "configuration",
    "budget",
    "repetition",
    "attempt",
    "outcome",
    "phase",
    "offeredRate",
    "sent",
    "completed",
    "withinThresholdFraction",
    "pending",
    "timeouts",
    "errors",
    "rejected",
    "p50Ms",
    "p95Ms",
    "p99Ms",
    "load",
    "sustainability",
    "workingSetMeanBytes",
    "workingSetDeltaFromIdle",
    "cpuMeanCores",
    "cpuDeltaFromIdle",
    "engineWorkingSetMeanBytes",
    "engineCpuMeanCores",
    "containersMean",
    "containersMax",
    "cpuSeconds",
    "memoryByteSeconds",
    "cpuSecondsPerUseful",
    "memoryByteSecondsPerUseful",
    "firstCorrectSeconds",
)


def write_experiment_02_report(
    root: Path, cells: Iterable[Experiment02Cell], protocol: Experiment02Protocol
) -> Path:
    """Write every cell's summary as JSON and one CSV row per cell and phase.

    Cells without a receipt are listed as incomplete, never as zero.
    """
    summaries: list[dict[str, Any]] = []
    for cell in cells:
        if (cell.run_dir(root) / CELL_RESULT).is_file():
            summaries.append(summarize_experiment_02_cell(cell, root, protocol))
        else:
            summaries.append(
                {
                    "cell": cell.label,
                    "configuration": cell.key,
                    "budget": cell.budget.label,
                    "repetition": cell.repetition,
                    "outcome": "incomplete",
                    "phases": [],
                }
            )
    (root / "experiment-02-summary.json").write_text(
        json.dumps(summaries, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path = root / "experiment-02-summary.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for summary in summaries:
            readiness = summary.get("readiness") or {}
            phases: list[dict[str, Any]] = summary["phases"] or [{}]
            for phase in phases:
                row = {key: phase.get(key) for key in _CSV_FIELDS}
                row.update(
                    configuration=summary["configuration"],
                    budget=summary["budget"],
                    repetition=summary["repetition"],
                    attempt=summary.get("attempt"),
                    outcome=summary["outcome"],
                    load=(phase.get("load") or {}).get("verdict"),
                    workingSetMeanBytes=(phase.get("workingSetBytes") or {}).get(
                        "mean"
                    ),
                    cpuMeanCores=(phase.get("cpuCores") or {}).get("mean"),
                    firstCorrectSeconds=readiness.get("firstCorrectSeconds"),
                )
                writer.writerow(row)
    return path
