"""How far each auction's forecast was from the load its node then received."""

import csv
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from nanolab.one_shot.models import CampaignManifest
from nanolab.tasks.one_shot.experiment import generator_rows
from nanolab.tasks.one_shot.trace import TraceArtifact

COLUMNS = (
    "epoch",
    "startsAtUtc",
    "endsAtUtc",
    "node",
    "function",
    "forecastSource",
    "predictedRate",
    "solverRate",
    "actualRequests",
    "actualRate",
    "scheduledRequests",
    "oracleRate",
    "error",
    "absoluteError",
    "relativeError",
    "errorVsOracle",
)
DEFINITION = (
    "error = predictedRate - actualRate, in requests/s: positive overestimates, "
    "negative underestimates. predictedRate is the forecastRate of the node's "
    "activated plan, the rate its auction used; solverRate is that rate floored "
    "to the flow quantum, as the solver received it. actualRate counts the "
    "requests the generator emitted from the node for the function during the "
    "interval, divided by its length; oracleRate is the frozen trace rate."
)


def solver_rate(rate: float, quantum: float) -> float:
    """Floor a non-oracle rate to the quantum grid, as NanoFaaS FlowUnits does."""
    load, step = Decimal(repr(rate)), Decimal(repr(quantum))
    return float((load // step) * step)


def forecast_error_rows(
    manifest: CampaignManifest,
    trace: TraceArtifact,
    *,
    schedule: list[dict],
    events: list[dict],
    epochs: list[dict],
) -> list[dict]:
    """One row per interval, edge node and function, whether or not a plan exists."""
    anchor = datetime.fromisoformat(manifest.anchor)
    period = trace.period_seconds
    count = len(trace.windows)
    plans = {row["epoch"]: row["nodes"] for row in epochs}
    declared = cast(list[dict], manifest.parameters.get("nodes") or [])
    edges = [node["id"] for node in declared if node["kind"] == "edge"] or sorted(
        {node for nodes in plans.values() for node in nodes}
    )
    emitted, seen = Counter(), set()
    for row in events:
        if row["event"] != "emitted" or row["originalId"] in seen:
            continue
        seen.add(row["originalId"])
        index = math.floor((row["emittedAt"] - anchor.timestamp()) / period)
        if 0 <= index < count:
            emitted[index, row["origin"], row["function"]] += 1
    scheduled = Counter(
        (row["epoch"], row["origin"], row["function"])
        for row in schedule
        if row["phase"] == "campaign"
    )
    rows = []
    for epoch in range(count):
        starts = anchor + timedelta(seconds=epoch * period)
        for node in edges:
            observed = (plans.get(epoch) or {}).get(node) or {}
            plan = (observed.get("result") or {}).get("plan") or {}
            for name in trace.functions:
                function = plan.get("functions", {}).get(name)
                actual = emitted[epoch, node, name] / period
                oracle = trace.windows[epoch].get(node, {}).get(name, 0.0)
                row = {
                    "epoch": epoch,
                    "startsAtUtc": starts.isoformat(),
                    "endsAtUtc": (starts + timedelta(seconds=period)).isoformat(),
                    "node": node,
                    "function": name,
                    "forecastSource": "plan"
                    if function
                    else "no-plan"
                    if epoch in plans
                    else "no-epoch",
                    "predictedRate": None,
                    "solverRate": None,
                    "actualRequests": emitted[epoch, node, name],
                    "actualRate": actual,
                    "scheduledRequests": scheduled[epoch, node, name],
                    "oracleRate": oracle,
                    "error": None,
                    "absoluteError": None,
                    "relativeError": None,
                    "errorVsOracle": None,
                }
                if function:
                    with_prediction(row, function["forecastRate"], plan["flowQuantum"])
                rows.append(row)
    return rows


def with_prediction(row: dict, predicted: float, quantum: float) -> dict:
    """Fill a row's prediction and its errors against the actual and oracle rates."""
    actual, error = row["actualRate"], predicted - row["actualRate"]
    row.update(
        predictedRate=predicted,
        solverRate=solver_rate(predicted, quantum),
        error=error,
        absoluteError=abs(error),
        relativeError=error / actual if actual else None,
        errorVsOracle=predicted - row["oracleRate"],
    )
    return row


def _statistics(rows: list[dict], period: float) -> dict:
    forecast = [row for row in rows if row["error"] is not None]
    errors = [row["error"] for row in forecast]
    over = [error for error in errors if error > 0]
    under = [error for error in errors if error < 0]
    return {
        "cells": len(rows),
        "forecasts": len(forecast),
        "meanError": statistics.fmean(errors) if errors else None,
        "meanAbsoluteError": statistics.fmean(map(abs, errors)) if errors else None,
        "rootMeanSquareError": math.sqrt(statistics.fmean(e * e for e in errors))
        if errors
        else None,
        "overestimates": {
            "count": len(over),
            "meanError": statistics.fmean(over) if over else None,
            "largestError": max(over, default=None),
        },
        "underestimates": {
            "count": len(under),
            "meanError": statistics.fmean(under) if under else None,
            "largestError": min(under, default=None),
        },
        "exact": len(errors) - len(over) - len(under),
        "predictedRequests": sum(row["predictedRate"] for row in forecast) * period,
        "actualRequests": sum(row["actualRequests"] for row in forecast),
    }


def summarize_forecast_errors(
    rows: list[dict], *, manifest: CampaignManifest, trace: TraceArtifact
) -> dict:
    """Signed and absolute errors overall, per node, function, pair and interval."""
    period = trace.period_seconds

    def group(*keys):
        groups: dict[tuple, list[dict]] = {}
        for row in rows:
            groups.setdefault(tuple(row[key] for key in keys), []).append(row)
        return groups

    by_pair: dict[str, dict[str, dict]] = {}
    for (node, name), cells in group("node", "function").items():
        by_pair.setdefault(node, {})[name] = _statistics(cells, period)
    return {
        "schemaVersion": 1,
        "forecastProvider": manifest.mode,
        "periodSeconds": period,
        "epochs": len(trace.windows),
        "definition": DEFINITION,
        "overall": _statistics(rows, period),
        "byNode": {
            node: _statistics(cells, period) for (node,), cells in group("node").items()
        },
        "byFunction": {
            name: _statistics(cells, period)
            for (name,), cells in group("function").items()
        },
        "byNodeFunction": by_pair,
        "byEpoch": {
            str(epoch): _statistics(cells, period)
            for (epoch,), cells in group("epoch").items()
        },
    }


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def record_forecast_errors(run_dir: Path, output: Path | None = None) -> dict:
    """Write forecast-errors.csv/json from a run's own files; never overwrite them."""
    output = output or run_dir
    manifest = CampaignManifest.model_validate_json(
        (run_dir / "manifest.json").read_bytes()
    )
    trace = TraceArtifact.model_validate_json((run_dir / "trace.json").read_bytes())
    rows = forecast_error_rows(
        manifest,
        trace,
        schedule=json.loads((run_dir / "schedule.json").read_text()),
        events=generator_rows(run_dir / "generator.jsonl"),
        epochs=_jsonl(run_dir / "epochs.jsonl"),
    )
    summary = summarize_forecast_errors(rows, manifest=manifest, trace=trace)
    with (output / "forecast-errors.csv").open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    with (output / "forecast-errors.json").open("x") as stream:
        json.dump(summary, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return summary
