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
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

from nanolab.comparison.cells import (
    CELL_RESULT,
    EVENTS,
    SUMMARY,
    attempt_dir,
    identity_sha256,
)
from nanolab.comparison.experiment_01 import (
    build_ledger,
    read_events,
    summarize_phases,
)
from nanolab.config.experiment_02 import Budget, Experiment02Protocol
from nanolab.tasks.experiment_02.budget import ENGINE_CGROUPS
from nanolab.tasks.experiment_02.samples import (
    CPU_WINDOW_SECONDS,
    OBSERVERS,
    ROOT,
    SAMPLE_INTERVAL_SECONDS,
    Samples,
    container_count,
    cpu_cores,
    cpu_seconds,
    memory_byte_seconds,
    read_cgroup_samples,
    window_covered,
    working_set,
)

Platform = Literal["nanofaas", "direct", "baseline"]

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
    # The baselines on the stack VM; they run the paper's qr-code wrappers.
    Configuration("funless", "FunLess 0.9.0", "baseline"),
    Configuration("spin", "Spin 4.2.5", "baseline"),
    Configuration("tinyfaas", "tinyFaaS eff00d0", "baseline"),
    Configuration("serverledge", "Serverledge 45419f5", "baseline"),
    Configuration("fn", "Fn 0.3.770", "baseline"),
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
    workloads: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return what the manifest records about experiment 02 beyond the scenario.

    `workloads` is the commit and tree of the paper's wrappers, when a
    baseline is selected.
    """
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
        "workloads": None if workloads is None else dict(workloads),
    }


# ---------------------------------------------------------------- summaries


Points = Sequence[tuple[float, float]]
_MEMORY = ("memoryCurrent", "inactiveFile")
_CPU = ("cpuUsageUsec",)


def _stats(points: Points, start: float, end: float, covered: bool) -> dict[str, Any]:
    """Return mean, maximum and count inside the window, or why there are none."""
    inside = [value for at, value in points if start <= at <= end]
    if not covered:
        return {"mean": None, "max": None, "samples": len(inside), "coverage": "gap"}
    if not inside:
        return {"mean": None, "max": None, "samples": 0, "coverage": "empty"}
    return {
        "mean": statistics.fmean(inside),
        "max": max(inside),
        "samples": len(inside),
        "coverage": "complete",
    }


def _resources(
    samples: Samples, cgroups: tuple[str, ...], start: float, end: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the working set and CPU rate of `cgroups` in one phase.

    Both need the phase covered by samples; a CPU rate also needs its 30 s
    look-back inside the phase, as experiment 01 requires.
    """
    memory = _stats(
        working_set(samples, cgroups),
        start,
        end,
        window_covered(samples, start, end, cgroups=cgroups, fields=_MEMORY),
    )
    cores = _stats(
        cpu_cores(samples, cgroups),
        start + CPU_WINDOW_SECONDS,
        end,
        window_covered(samples, start, end, cgroups=cgroups, fields=_CPU),
    )
    return memory, cores


def _delta(
    value: Mapping[str, Any], reference: Mapping[str, Any] | None
) -> float | None:
    if reference is None or value["mean"] is None or reference["mean"] is None:
        return None
    return float(value["mean"]) - float(reference["mean"])


def _containers(samples: Samples, start: float, end: float) -> dict[str, Any]:
    """Return the live instances in the phase: complete scans only."""
    if not window_covered(samples, start, end):
        return {"containersMean": None, "containersMax": None}
    counts = [count for at, count in container_count(samples) if start <= at <= end]
    return {
        "containersMean": statistics.fmean(counts) if counts else None,
        "containersMax": max(counts) if counts else None,
    }


def _efficiency(
    samples: Samples, useful: int, start: float, end: float, origin: float
) -> dict[str, Any]:
    """Return the deployment's CPU time and memory occupancy per useful response.

    Over the cohort's arrival window and the drain after it, as §4.5 of the
    paper defines it: the windows of neighbouring steps overlap, so the values
    are not additive and are not an exclusive attribution to the cohort.
    """
    cpu = cpu_seconds(samples, start, end)
    memory = memory_byte_seconds(samples, start, end)
    return {
        "efficiencyStartSeconds": start - origin,
        "efficiencyEndSeconds": end - origin,
        "cpuSeconds": cpu,
        "memoryByteSeconds": memory,
        # Undefined, not zero, when nothing useful came back.
        "cpuSecondsPerUseful": cpu / useful if useful and cpu is not None else None,
        "memoryByteSecondsPerUseful": (
            memory / useful if useful and memory is not None else None
        ),
    }


def _scheduler_coverage(
    configuration: Configuration, path: Path, start: float, end: float
) -> dict[str, Any]:
    """Say whether nanoFaaS's exposition was scraped over the whole measurement."""
    if configuration.platform != "nanofaas":
        return {"schedulerCoverage": "not_applicable", "schedulerSamples": None}
    if not path.is_file():
        return {"schedulerCoverage": "partial", "schedulerSamples": None}
    times: list[float] = []
    failed = False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            scrape = json.loads(line)
        except ValueError:
            failed = True
            continue
        at = float(scrape["ts"])
        if scrape.get("status") == "ok":
            times.append(at)
        # Refused scrapes before the platform is up are expected; only the
        # measured calendar must be covered.
        elif start <= at <= end:
            failed = True
    times.sort()
    first = bisect_right(times, start) - 1
    last = bisect_left(times, end)
    covered = (
        first >= 0
        and last < len(times)
        and all(
            later - earlier <= 2 * SAMPLE_INTERVAL_SECONDS
            for earlier, later in pairwise(times[first : last + 1])
        )
    )
    return {
        "schedulerCoverage": "complete" if covered and not failed else "partial",
        "schedulerSamples": SCHEDULER,
    }


def summarize_experiment_02_cell(
    cell: Experiment02Cell, root: Path, protocol: Experiment02Protocol
) -> dict[str, object]:
    """Summarise one cell from the attempt its receipt selects.

    Experiment 01's per-phase rows and verdicts, from its ledger alone, plus
    what experiment 02 adds from the cgroup samples: the deployment's working
    set and CPU, the engine's share, the agents' own cost, the live instances,
    and CPU time and memory occupancy per useful response. A resource the
    samples do not cover is unknown with its reason, and the ledger's service
    values stay usable.
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
    read = read_cgroup_samples(selected / SAMPLES)
    samples = read.samples
    rows = summarize_phases(
        protocol,
        ledger,
        summary=json.loads((selected / SUMMARY).read_text(encoding="utf-8")),
        snapshot=None,
        malformed_events=malformed,
    )
    phases = protocol.phases()
    origin = None if ledger["origin"] is None else ledger["origin"] / 1000
    idle: dict[str, Any] = {}
    for row, phase in zip(rows, phases, strict=True):
        if origin is None:
            continue
        start = origin + phase["startSeconds"]
        end = start + phase["durationSeconds"]
        memory, cores = _resources(samples, ROOT, start, end)
        engine_memory, engine_cores = _resources(samples, ENGINE_CGROUPS, start, end)
        observer_memory, observer_cores = _resources(samples, OBSERVERS, start, end)
        if phase["name"] == "idle":
            idle = {"memory": memory, "cpu": cores}
        row.update(
            workingSetBytes=memory,
            cpuCores=cores,
            workingSetDeltaFromIdle=_delta(memory, idle.get("memory")),
            cpuDeltaFromIdle=_delta(cores, idle.get("cpu")),
            engineWorkingSetMeanBytes=engine_memory["mean"],
            engineCpuMeanCores=engine_cores["mean"],
            observersWorkingSetMeanBytes=observer_memory["mean"],
            observersCpuMeanCores=observer_cores["mean"],
            coverage={
                "deployment": memory["coverage"],
                "engine": engine_memory["coverage"],
                "observers": observer_memory["coverage"],
            },
            **_containers(samples, start, end),
        )
        if phase["rate"] > 0:
            row.update(
                _efficiency(
                    samples,
                    int(row["withinThreshold"]),
                    start,
                    end + protocol.drain_seconds,
                    origin,
                )
            )
    if origin is not None:
        last = phases[-1]
        result |= _scheduler_coverage(
            cell.configuration,
            selected / SCHEDULER,
            origin,
            origin + last["startSeconds"] + last["durationSeconds"],
        )
    result["malformedEvents"] = malformed
    result["malformedSamples"] = read.malformed
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
    "workingSetMaxBytes",
    "workingSetDeltaFromIdle",
    "cpuMeanCores",
    "cpuMaxCores",
    "cpuDeltaFromIdle",
    "deploymentCoverage",
    "engineWorkingSetMeanBytes",
    "engineCpuMeanCores",
    "observersWorkingSetMeanBytes",
    "observersCpuMeanCores",
    "containersMean",
    "containersMax",
    "efficiencyStartSeconds",
    "efficiencyEndSeconds",
    "cpuSeconds",
    "memoryByteSeconds",
    "cpuSecondsPerUseful",
    "memoryByteSecondsPerUseful",
    "apiReadySeconds",
    "registeredSeconds",
    "firstCorrectSeconds",
    "schedulerCoverage",
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
                memory = phase.get("workingSetBytes") or {}
                cores = phase.get("cpuCores") or {}
                row.update(
                    configuration=summary["configuration"],
                    budget=summary["budget"],
                    repetition=summary["repetition"],
                    attempt=summary.get("attempt"),
                    outcome=summary["outcome"],
                    load=(phase.get("load") or {}).get("verdict"),
                    workingSetMeanBytes=memory.get("mean"),
                    workingSetMaxBytes=memory.get("max"),
                    cpuMeanCores=cores.get("mean"),
                    cpuMaxCores=cores.get("max"),
                    deploymentCoverage=(phase.get("coverage") or {}).get("deployment"),
                    apiReadySeconds=readiness.get("apiReadySeconds"),
                    registeredSeconds=readiness.get("registeredSeconds"),
                    firstCorrectSeconds=readiness.get("firstCorrectSeconds"),
                    schedulerCoverage=summary.get("schedulerCoverage"),
                )
                writer.writerow(row)
    return path
