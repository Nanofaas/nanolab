"""Run-level experiment 02 estimates, with explicit failures and missing values."""

from __future__ import annotations

import csv
import math
import random
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import TypeAdapter

from nanolab.comparison.cells import (
    attempt_dir,
    identity_sha256,
    read_cell_result,
    sha256_file,
    write_json_atomically,
)
from nanolab.comparison.experiment_02 import (
    EXPERIMENT_02_ORDER_POLICY,
    Configuration,
    Experiment02Cell,
    build_experiment_02_matrix,
    summarize_experiment_02_cell,
)
from nanolab.comparison.manifest import read_comparison_manifest
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
    "sentThroughput",
    "scheduled",
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
        "droppedIterations": (row.get("load") or {}).get("dropped"),
        "generatorStartDelaySeconds": (row.get("load") or {}).get("startDelaySeconds"),
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
        "droppedIterations",
        "generatorStartDelaySeconds",
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


_ARCHIVED_INPUTS = {
    "payload": "inputs/experiment-02-payload.json",
    "generator": "inputs/experiment-02-generator.js",
}


def archive_experiment_02_inputs(root: Path, *, payload: bytes, script: Path) -> None:
    """Save exact, non-secret measurement inputs without replacing old bytes."""
    for name, content in (("payload", payload), ("generator", script.read_bytes())):
        path = root / _ARCHIVED_INPUTS[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError(f"archived input differs: {path}") from None


def _write_csv(path: Path, rows: list[dict[str, Any]], context: dict[str, Any]) -> None:
    flattened = []
    for row in rows:
        values = {
            **context,
            **{k: v for k, v in row.items() if k not in {"outcomes", "metrics"}},
            **row["outcomes"],
        }
        for name, metric in row["metrics"].items():
            interval = metric["confidence95"]
            values.update(
                {
                    f"{name}.mean": metric["mean"],
                    f"{name}.lower95": interval[0] if interval is not None else None,
                    f"{name}.upper95": interval[1] if interval is not None else None,
                    f"{name}.sampleCount": metric["sampleCount"],
                    f"{name}.missingRuns": metric["missingRuns"],
                }
            )
        flattened.append(values)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flattened[0]))
        writer.writeheader()
        writer.writerows(flattened)


def write_experiment_02_campaign_report(root: Path) -> Path:
    """Re-analyse a saved run locally, leaving raw data and receipts unchanged."""
    manifest = read_comparison_manifest(root)
    if manifest is None:
        raise ValueError("comparison manifest required")
    identity = cast(dict[str, Any], manifest.identity)
    experiment = identity.get("experiment")
    if not isinstance(experiment, dict) or experiment.get("profile") != "experiment-02":
        raise ValueError("report requires an experiment-02 run")
    protocol = Experiment02Protocol.model_validate(experiment["protocol"])
    if experiment["payload"]["sha256"] != protocol.payload_sha256:
        raise ValueError("recorded payload identities disagree")
    adapter = TypeAdapter(Configuration)
    configurations = tuple(
        adapter.validate_python({"key": key, **experiment["configurations"][key]})
        for key in identity["variants"]
    )
    repetitions = identity["repetitions"]
    cells = build_experiment_02_matrix(
        configurations, tuple(protocol.budgets), repetitions, seed=protocol.order_seed
    )
    if (
        experiment["orderPolicy"] != EXPERIMENT_02_ORDER_POLICY
        or [cell.label for cell in cells] != experiment["order"]
    ):
        raise ValueError("recorded matrix order is inconsistent or unsupported")
    archived = {}
    for name, relative in _ARCHIVED_INPUTS.items():
        path = root / relative
        expected = experiment["payload" if name == "payload" else "script"]["sha256"]
        if path.exists() and sha256_file(path) != expected:
            raise ValueError(f"archived input differs: {relative}")
        archived[name] = relative if path.is_file() else None
    summaries = []
    raw = []
    for cell in cells:
        receipt = read_cell_result(cell, root, identity)
        files = {}
        if receipt is None:
            summary = {
                "configuration": cell.key,
                "budget": cell.budget.label,
                "repetition": cell.repetition,
                "outcome": "incomplete",
                "phases": [],
            }
        else:
            summary = summarize_experiment_02_cell(cell, root, protocol)
            selected = attempt_dir(cell.run_dir(root), int(receipt["attempt"]))
            files = {
                str((selected / name).relative_to(root)): digest
                for name, digest in receipt["evidence"].items()
            }
        summaries.append(summary)
        raw.append({"cell": cell.label, "outcome": summary["outcome"], "files": files})
    result = aggregate_experiment_02(
        summaries, cells, protocol, repetitions=repetitions
    )
    result.update(
        schemaVersion=1,
        measurement=manifest.model_dump(mode="json"),
        archivedInputs=archived,
        rawArtifacts=raw,
        pathsRelativeTo="run directory",
        estimatesPopulation="available measured runs",
    )
    environment = identity["environment"]["values"]
    context = {
        "measurementIdentitySha256": identity_sha256(identity),
        "protocolSha256": experiment["protocolSha256"],
        "payloadSha256": protocol.payload_sha256,
        "workloadCommit": (experiment.get("workloads") or {}).get("commit"),
        "nanofaasRevision": identity["nanofaas"]["revision"],
        "measurementToolRevision": identity["nanolab"]["revision"],
        "hostImage": (environment.get("azure") or {}).get("image_urn"),
        "hostSize": (environment.get("azure") or {}).get("vm_size"),
    }
    output = root / "analysis"
    path = output / "experiment-02-campaign.json"
    write_json_atomically(path, result)
    _write_csv(output / "experiment-02-reference.csv", result["reference"], context)
    _write_csv(output / "experiment-02-budget-grid.csv", result["budgetGrid"], context)
    return path
