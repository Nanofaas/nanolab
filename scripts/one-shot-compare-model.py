#!/usr/bin/env python3
"""Compare a continuous one-shot run with the Python model's predictions.

The model (the pinned original helpers, solved on the frozen trace's rates)
predicts, minute by minute, how each edge splits its requests: served locally,
granted to each neighbour, requested from the cloud. The run's generator
records where every original executed (`executionNode`), so the measured split
needs no physical proof. Both sides are counted in requests per minute; the
run's rejected and failed originals have no model counterpart and are reported
beside the split. The model plans with the exact rates while the run plans
with its forecasts, so differences are expected and are recorded, not judged.
Writes model-comparison.json and model-comparison.md into --output.
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

KINDS = ("local", "peer", "cloud")


def generator_results(path: Path) -> list[dict]:
    """Return the campaign's result events from a k6 generator log."""
    rows = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(json.loads(line)["msg"])
        except (ValueError, KeyError, TypeError):
            continue
        if row.get("event") == "result" and row.get("phase") == "campaign":
            rows.append(row)
    return rows


def predicted(selected: list[dict], period: float):
    """Requests the model routes per minute: per origin and kind, and per route."""
    split = [defaultdict(lambda: dict.fromkeys(KINDS, 0.0)) for _ in selected]
    routes = [defaultdict(float) for _ in selected]
    for minute, result in enumerate(selected):
        context = result["context"]
        scale = context["flowQuantum"] * period
        for i, origin in enumerate(context["edges"]):
            split[minute][origin]["local"] += sum(result["localUnits"][i]) * scale
            split[minute][origin]["cloud"] += (
                sum(result["cloudRequestedUnits"][i]) * scale
            )
            for j, destination in enumerate(context["edges"]):
                granted = sum(result["neighborUnits"][i][j]) * scale
                if granted:
                    split[minute][origin]["peer"] += granted
                    routes[minute][origin, destination] += granted
    return split, routes


def measured(rows: list[dict], minutes: int, cloud: str):
    """Requests the run routed per minute, by where each original executed."""
    split = [
        defaultdict(lambda: dict.fromkeys((*KINDS, "rejected", "failed"), 0))
        for _ in range(minutes)
    ]
    routes = [defaultdict(int) for _ in range(minutes)]
    for row in rows:
        origin, minute = row["origin"], row["epoch"]
        if origin == cloud:
            continue
        cell = split[minute][origin]
        succeeded = (
            row.get("statusCode") == 200
            and str(row.get("responseStatus")).lower() == "success"
        )
        if not succeeded:
            cell["rejected" if row.get("statusCode") == 429 else "failed"] += 1
            continue
        destination = row.get("executionNode") or origin
        if destination == origin:
            cell["local"] += 1
        elif destination == cloud:
            cell["cloud"] += 1
        else:
            cell["peer"] += 1
            routes[minute][origin, destination] += 1
    return split, routes


def relative(model: float, run: float) -> float | None:
    """Return the run's relative difference; None where the model has nothing."""
    return None if model == 0 else (run - model) / model


def percent_difference(model: float, run: float) -> float | None:
    """Return the symmetric relative difference, (run - model) / their mean.

    It stays within ±2 and is defined when only one side is zero, so a small
    model count does not inflate it; None when both are zero.
    """
    mean = (model + run) / 2
    return None if mean == 0 else (run - model) / mean


def share(requests: float, load: float) -> float | None:
    """Return requests as a fraction of the offered load; None without load."""
    return None if load == 0 else requests / load


def correlation(model: list[float], run: list[float]) -> float | None:
    """Pearson correlation of two minute series; None when one is constant."""
    try:
        return statistics.correlation(model, run)
    except statistics.StatisticsError:
        return None


def compare(selected: list[dict], trace: dict, rows: list[dict]) -> dict:
    """Build the model-against-run document for one continuous run."""
    windows = trace["windows"]
    if [result["rates"] for result in selected] != windows:
        raise SystemExit("the model was not solved on this run's trace")
    roles = trace.get("node_roles") or {}
    cloud = next(node for node, role in roles.items() if role == "cloud-direct")
    edges = [node for node in trace["nodes"] if node != cloud]
    minutes = len(windows)
    model_split, model_routes = predicted(selected, trace["period_seconds"])
    run_split, run_routes = measured(rows, minutes, cloud)

    def total(split, origin, kind):
        return sum(split[minute][origin][kind] for minute in range(minutes))

    origins = []
    for origin in edges:
        row = {"origin": origin}
        for kind in KINDS:
            model, run = (
                total(model_split, origin, kind),
                total(run_split, origin, kind),
            )
            row[kind] = {
                "model": round(model, 2),
                "run": run,
                "relativeDifference": relative(model, run),
                "relativePercentDifference": percent_difference(model, run),
            }
        row["rejected"] = total(run_split, origin, "rejected")
        row["failed"] = total(run_split, origin, "failed")
        # Every original from the origin, served or not: the base of the shares.
        row["load"] = (
            sum(row[kind]["run"] for kind in KINDS) + row["rejected"] + row["failed"]
        )
        for kind in KINDS:
            row[kind]["loadShareDifference"] = share(
                row[kind]["run"] - row[kind]["model"], row["load"]
            )
        row["notServedShare"] = share(row["rejected"] + row["failed"], row["load"])
        origins.append(row)
    pairs = sorted(
        {pair for minute in model_routes for pair in minute}
        | {pair for minute in run_routes for pair in minute}
    )
    routes = []
    for origin, destination in pairs:
        model = sum(m[origin, destination] for m in model_routes)
        run = sum(m[origin, destination] for m in run_routes)
        routes.append(
            {
                "origin": origin,
                "destination": destination,
                "model": round(model, 2),
                "run": run,
                "relativePercentDifference": percent_difference(model, run),
            }
        )
    series = {
        kind: {
            "model": [
                round(sum(model_split[m][o][kind] for o in edges), 2)
                for m in range(minutes)
            ],
            "run": [sum(run_split[m][o][kind] for o in edges) for m in range(minutes)],
        }
        for kind in KINDS
    }
    series["notServed"] = {
        "run": [
            sum(run_split[m][o]["rejected"] + run_split[m][o]["failed"] for o in edges)
            for m in range(minutes)
        ]
    }
    for kind in KINDS:
        model, run = series[kind]["model"], series[kind]["run"]
        series[kind]["correlation"] = correlation(model, run)
        series[kind]["meanAbsoluteError"] = statistics.fmean(
            abs(a - b) for a, b in zip(model, run, strict=True)
        )
    load = sum(row["load"] for row in origins)
    return {
        "minutes": minutes,
        "load": load,
        "periodSeconds": trace["period_seconds"],
        "edges": edges,
        "cloud": cloud,
        "system": {
            kind: {
                "model": round(sum(series[kind]["model"]), 2),
                "run": sum(series[kind]["run"]),
                "relativeDifference": relative(
                    sum(series[kind]["model"]), sum(series[kind]["run"])
                ),
                "relativePercentDifference": percent_difference(
                    sum(series[kind]["model"]), sum(series[kind]["run"])
                ),
                "loadShareDifference": share(
                    sum(series[kind]["run"]) - sum(series[kind]["model"]), load
                ),
            }
            for kind in KINDS
        }
        | {
            "notServed": {
                "run": sum(series["notServed"]["run"]),
                "loadShare": share(sum(series["notServed"]["run"]), load),
            }
        },
        "origins": origins,
        "routes": routes,
        "series": series,
    }


def delta(model: float, run: float) -> str:
    """Format the run's relative difference from the model."""
    if model == 0:
        return "" if run == 0 else "model 0"
    return f"{(run - model) / model:+.1%}"


def points(fraction: float | None) -> str:
    """Format a fraction of the load in signed percentage points."""
    return "" if fraction is None else f"{fraction * 100:+.2f} pp"


def rpd(model: float, run: float) -> str:
    """Format the symmetric relative percent difference."""
    value = percent_difference(model, run)
    return "" if value is None else f"{value:+.1%}"


def markdown(document: dict, *, run: Path, selected: Path) -> str:
    """Render the comparison for review."""
    lines = [
        "# Continuous ARI run against the Python model",
        "",
        f"Run: `{run}`  ",
        f"Model: `{selected}`",
        "",
        (
            "Requests over the measurement, originals from the edges. The model "
            "plans on the trace's exact rates, the run on its forecasts; the "
            "run's rejected and failed originals have no model counterpart. "
            "Δ is relative to the model; RPD is the symmetric relative percent "
            "difference, (run − model) / mean(run, model), within ±200% and "
            "defined where the model has nothing. Δ load is the difference in "
            "percentage points of the offered load (every original from the "
            "edges, or from the origin): across local, peer, cloud and not "
            "served it sums to zero."
        ),
        "",
        "## System",
        "",
        (
            "| Kind | Model | Run | Δ | RPD | Δ load | Minute correlation "
            "| Mean abs. error/min |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for kind in KINDS:
        row, series = document["system"][kind], document["series"][kind]
        r = series["correlation"]
        lines.append(
            f"| {kind} | {row['model']:.0f} | {row['run']} | "
            f"{delta(row['model'], row['run'])} | "
            f"{rpd(row['model'], row['run'])} | "
            f"{points(row['loadShareDifference'])} | "
            f"{'' if r is None else f'{r:.2f}'} | "
            f"{series['meanAbsoluteError']:.1f} |"
        )
    served = document["system"]["notServed"]
    lines.append(
        f"| not served | — | {served['run']} | | | {points(served['loadShare'])} | | |"
    )
    lines += [
        "",
        "## Per origin",
        "",
        (
            "| Origin | Local model | Local run | Δ | RPD | Peer model | Peer run "
            "| Δ | RPD | Cloud model | Cloud run | Δ | RPD | Rejected | Failed |"
        ),
        "|---|" + "---:|" * 14,
    ]
    for row in document["origins"]:
        cells = [
            f"{row[k]['model']:.0f} | {row[k]['run']} | "
            f"{delta(row[k]['model'], row[k]['run'])} | "
            f"{rpd(row[k]['model'], row[k]['run'])}"
            for k in KINDS
        ]
        lines.append(
            f"| {row['origin']} | "
            + " | ".join(cells)
            + f" | {row['rejected']} | {row['failed']} |"
        )
    lines += [
        "",
        "## Per origin, in points of the origin's load",
        "",
        "| Origin | Load | Local | Peer | Cloud | Not served |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in document["origins"]:
        cells = [points(row[k]["loadShareDifference"]) for k in KINDS]
        lines.append(
            f"| {row['origin']} | {row['load']} | "
            + " | ".join(cells)
            + f" | {points(row['notServedShare'])} |"
        )
    lines += [
        "",
        "## Routes between edges",
        "",
        "| Origin → destination | Model | Run | Δ | RPD |",
        "|---|---:|---:|---:|---:|",
    ]
    for route in document["routes"]:
        lines.append(
            f"| {route['origin']} → {route['destination']} | {route['model']:.0f} "
            f"| {route['run']} | {delta(route['model'], route['run'])} "
            f"| {rpd(route['model'], route['run'])} |"
        )
    lines += [
        "",
        "## Per minute (requests)",
        "",
        "| Minute | Peer model | Peer run | Cloud model | Cloud run | Not served |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    series = document["series"]
    for minute in range(document["minutes"]):
        lines.append(
            f"| {minute} | {series['peer']['model'][minute]:.0f} "
            f"| {series['peer']['run'][minute]} "
            f"| {series['cloud']['model'][minute]:.0f} "
            f"| {series['cloud']['run'][minute]} "
            f"| {series['notServed']['run'][minute]} |"
        )
    return "\n".join(lines) + "\n"


def main(argv=None):
    """Compare one run directory with the model's selected predictions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="run directory (generator, trace)")
    parser.add_argument("selected", type=Path, help="the model's selected.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    document = compare(
        json.loads(args.selected.read_text()),
        json.loads((args.run / "trace.json").read_text()),
        generator_results(args.run / "generator.jsonl"),
    )
    document |= {"run": str(args.run), "selected": str(args.selected)}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "model-comparison.json").write_text(
        json.dumps(document, indent=2) + "\n"
    )
    (args.output / "model-comparison.md").write_text(
        markdown(document, run=args.run, selected=args.selected)
    )
    print(args.output / "model-comparison.md")


if __name__ == "__main__":
    main()
