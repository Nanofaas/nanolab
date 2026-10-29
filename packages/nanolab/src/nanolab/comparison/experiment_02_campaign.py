"""Run-level experiment 02 estimates, with explicit failures and missing values."""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from nanolab.comparison.experiment_02 import Experiment02Cell
from nanolab.config.experiment_02 import Experiment02Protocol
from nanolab.tasks.one_shot.statistics import quantile

BOOTSTRAP_DRAWS = 10000
_OUTCOMES = ("measured", "oom", "startup_failed", "incomplete")
_PHASE_METRICS = (
    "sent",
    "completed",
    "correct",
    "incorrect",
    "rejected",
    "errors",
    "timeouts",
    "pending",
    "correctThroughput",
    "successFraction",
    "p50Ms",
    "p95Ms",
    "p99Ms",
    "cpuSecondsPerUseful",
    "memoryByteSecondsPerUseful",
    "engineWorkingSetMeanBytes",
    "engineCpuMeanCores",
    "observersWorkingSetMeanBytes",
    "observersCpuMeanCores",
)


def _estimate(values: Sequence[float | None], seed: str) -> dict[str, Any]:
    observed = [float(value) for value in values if value is not None]
    if any(not math.isfinite(value) for value in observed):
        raise ValueError("cannot aggregate a non-finite metric")
    interval = None
    if len(observed) > 1:
        if min(observed) == max(observed):
            interval = [observed[0], observed[0]]
        else:
            rng = random.Random(seed)  # nosec B311: reproducible statistical bootstrap
            means = [
                statistics.fmean(rng.choices(observed, k=len(observed)))
                for _ in range(BOOTSTRAP_DRAWS)
            ]
            interval = [quantile(means, 0.025), quantile(means, 0.975)]
    return {
        "mean": statistics.fmean(observed) if observed else None,
        "confidence95": interval,
        "sampleCount": len(observed),
        "missingRuns": len(values) - len(observed),
    }


def _metrics(summary: Mapping[str, Any] | None, phase: str) -> dict[str, Any]:
    if summary is None or summary["outcome"] != "measured":
        return {}
    phases = {row["phase"]: row for row in summary["phases"]}
    row = phases.get(phase, {})
    idle = phases.get("idle", {})
    idle_bytes = (idle.get("workingSetBytes") or {}).get("mean")
    return {
        **{key: row.get(key) for key in _PHASE_METRICS},
        "idleMemoryMiB": idle_bytes / 1048576 if idle_bytes is not None else None,
        "idleCpuCores": (idle.get("cpuCores") or {}).get("mean"),
        "readinessSeconds": (summary.get("readiness") or {}).get("firstCorrectSeconds"),
        "workingSetMeanBytes": (row.get("workingSetBytes") or {}).get("mean"),
        "cpuMeanCores": (row.get("cpuCores") or {}).get("mean"),
    }


def aggregate_experiment_02(
    summaries: Sequence[Mapping[str, Any]],
    cells: Sequence[Experiment02Cell],
    protocol: Experiment02Protocol,
    *,
    repetitions: int,
) -> dict[str, Any]:
    """Aggregate run estimates, not requests; never invent values for failed runs."""
    if repetitions < 1 or not cells:
        raise ValueError("aggregation needs a nonempty planned scope")
    expected = {(cell.key, cell.budget.label, cell.repetition) for cell in cells}
    if len(expected) != len(cells):
        raise ValueError("duplicate planned cell")
    groups = sorted({(cell.key, cell.budget.label) for cell in cells})
    if any(
        {rep for key, budget, rep in expected if (key, budget) == group}
        != set(range(1, repetitions + 1))
        for group in groups
    ):
        raise ValueError("incomplete planned repetition scope")
    observed = {}
    for summary in summaries:
        key = (summary["configuration"], summary["budget"], summary["repetition"])
        if key in observed:
            raise ValueError("duplicate repetition in summaries")
        if key not in expected or summary["outcome"] not in _OUTCOMES:
            raise ValueError("summary is outside the planned scope")
        if summary["outcome"] == "measured" and (
            summary.get("calendarOriginMs") is None
            or summary.get("malformedEvents", 0)
            or summary.get("anomalies")
        ):
            raise ValueError(f"unusable request ledger for {key}")
        observed[key] = summary
    rows = []
    phases = [phase for phase in protocol.phases() if phase["name"].startswith("step-")]
    metric_names = (
        *_PHASE_METRICS,
        "idleMemoryMiB",
        "idleCpuCores",
        "readinessSeconds",
        "workingSetMeanBytes",
        "cpuMeanCores",
    )
    configurations = {cell.key: cell.configuration for cell in cells}
    for key, budget in groups:
        runs = [observed.get((key, budget, rep)) for rep in range(1, repetitions + 1)]
        outcomes = dict.fromkeys(_OUTCOMES, 0)
        for run in runs:
            outcomes[run["outcome"] if run is not None else "incomplete"] += 1
        for phase in phases:
            name = phase["name"]
            metrics = [_metrics(run, name) for run in runs]
            rows.append(
                {
                    "configuration": key,
                    "label": configurations[key].label,
                    "profile": configurations[key].profile,
                    "budget": budget,
                    "phase": name,
                    "offeredRate": phase["rate"],
                    "expectedRuns": repetitions,
                    "outcomes": dict(outcomes),
                    "metrics": {
                        metric: _estimate(
                            [value.get(metric) for value in metrics],
                            f"{protocol.order_seed}:{key}:{budget}:{name}:{metric}",
                        )
                        for metric in metric_names
                    },
                }
            )
    return {
        "serviceDefinition": "correct-response",
        "latencyPopulation": "correct responses of the send cohort",
        "bootstrap": {
            "method": "percentile-95",
            "unit": "run",
            "draws": BOOTSTRAP_DRAWS,
            "seed": protocol.order_seed,
        },
        "referenceBudget": protocol.reference_budget.label,
        "reference": [
            row for row in rows if row["budget"] == protocol.reference_budget.label
        ],
        "budgetGrid": rows,
    }
