"""Measured workflow reports; incomplete observations never qualify a comparison."""

from __future__ import annotations

import html
import json
import random
import statistics
from pathlib import Path
from typing import Any, cast

import plotly.graph_objects as go

from nanolab.one_shot.models import CampaignManifest
from nanolab.tasks.one_shot.conservation import RunEvidence
from nanolab.tasks.one_shot.statistics import quantile


def summarize(
    manifest: CampaignManifest, evidence: RunEvidence | None
) -> dict[str, Any]:
    """Use physical completions for throughput and retain all unresolved originals."""
    conforming = bool(
        evidence is not None
        and evidence.valid
        and evidence.planned > 0
        and evidence.emitted == evidence.planned
        and evidence.completed + evidence.errors == evidence.planned
        and not evidence.censored
        and not evidence.generator_deficit
        and evidence.observations.get("loadExecuted") is not False
    )
    result: dict[str, Any] = {
        "provider": manifest.provider,
        "purpose": manifest.purpose,
        "mode": manifest.mode,
        "conforming": conforming,
        "missingEvidence": evidence is None,
        "manifest": manifest.model_dump(mode="json"),
    }
    result.update(
        {
            key: getattr(evidence, key) if evidence else None
            for key in (
                "planned",
                "emitted",
                "received",
                "attempts",
                "completed",
                "errors",
                "censored",
                "generator_deficit",
                "violations",
            )
        }
    )
    observations = cast(dict[str, Any], evidence.observations) if evidence else {}
    parameters = cast(dict[str, Any], manifest.parameters)
    windows = observations.get("plannedWindows")
    duration = (
        manifest.period_seconds * len(windows) if isinstance(windows, list) else 0
    )
    result["throughputOriginalsPerSecond"] = (
        evidence.completed / duration if evidence and duration else None
    )
    routing = {"local": 0, "peer": 0, "cloud": 0}
    cloud = {
        row["id"] for row in parameters.get("nodes", []) if row.get("kind") == "cloud"
    } or {"cloud"}
    if evidence:
        for cell in evidence.cells:
            destinations = cell.get("destinations", {})
            if isinstance(destinations, dict):
                for destination, count in destinations.items():
                    category = (
                        "cloud"
                        if destination in cloud
                        else "local"
                        if destination == cell["origin"]
                        else "peer"
                    )
                    routing[category] += int(cast(int, count))
    result["routing"] = routing if evidence else None
    latencies = observations.get("latenciesSeconds", [])
    result["latencySeconds"] = (
        {f"p{int(p * 100)}": quantile(latencies, p) for p in (0.5, 0.95)}
        if latencies
        else None
    )
    result["realizedUtility"] = (
        observations.get("realizedUtility") if conforming else None
    )
    result["utilityConvention"] = observations.get("utilityConvention")
    result["telemetry"] = {
        key: observations.get(key)
        for key in ("metricSamples", "epochs", "physicalServiceByNodeFunction")
    }
    result["telemetryUnavailable"] = [
        key for key, value in result["telemetry"].items() if not value
    ]
    result["auctionEpochs"] = [
        {
            "epoch": row["epoch"],
            "censored": row.get("censored"),
            "readySeconds": row.get("globalReadyWallSeconds"),
            "auctionToPeriodRatio": row["auctionWallUpperBoundSeconds"]
            / manifest.period_seconds
            if row.get("auctionWallUpperBoundSeconds") is not None
            else None,
        }
        for row in observations.get("epochs", [])
    ]
    result["transitions"] = [
        {
            "epoch": epoch["epoch"],
            "node": node,
            "desiredReplicas": (row.get("result") or {}).get("desiredReplicas"),
            "readyReplicas": (row.get("result") or {}).get("readyReplicas"),
        }
        for epoch in observations.get("epochs", [])
        for node, row in epoch.get("nodes", {}).items()
    ]
    return result


def _write(summary: dict[str, Any], output: Path, title: str) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, indent=2, allow_nan=False)
    output.with_suffix(".json").write_text(text + "\n")
    rows = summary.get("runs", [summary])
    figure = go.Figure()
    for key in ("completed", "errors", "censored", "generator_deficit"):
        figure.add_bar(
            name=key, x=[row["mode"] for row in rows], y=[row.get(key) for row in rows]
        )
    figure.update_layout(title=title, barmode="stack", yaxis_title="Original requests")
    charts = [figure.to_html(full_html=False, include_plotlyjs=True)]
    routing = go.Figure()
    for category in ("local", "peer", "cloud"):
        routing.add_bar(
            name=category,
            x=[row["mode"] for row in rows],
            y=[
                row["routing"].get(category) if row.get("routing") else None
                for row in rows
            ],
        )
    routing.update_layout(title="Physical completions by destination", barmode="stack")
    charts.append(routing.to_html(full_html=False, include_plotlyjs=False))
    for row in rows:
        epochs = row.get("auctionEpochs", [])
        if epochs:
            wall = go.Figure(
                go.Scatter(
                    x=[epoch["epoch"] for epoch in epochs],
                    y=[epoch["auctionToPeriodRatio"] for epoch in epochs],
                    name="T_auction / T",
                )
            )
            wall.update_layout(
                title=f"{row['mode']}: conservative auction wall / period"
            )
            charts.append(wall.to_html(full_html=False, include_plotlyjs=False))
        series: dict[tuple, list] = {}
        for sample in row.get("telemetry", {}).get("metricSamples") or []:
            for name, value in sample["metrics"].items():
                key = (
                    sample["node"],
                    sample.get("function"),
                    sample.get("replica"),
                    name,
                )
                series.setdefault(key, []).append((sample["at"], value))
        if series:
            metrics = go.Figure()
            for key, values in series.items():
                metrics.add_scatter(
                    x=[point[0] for point in values],
                    y=[point[1] for point in values],
                    name="/".join(str(part) for part in key if part is not None),
                    visible="legendonly",
                )
            metrics.update_layout(
                title=(
                    f"{row['mode']}: queues, handlers, utilization and protocol "
                    "metrics (select a series)"
                )
            )
            charts.append(metrics.to_html(full_html=False, include_plotlyjs=False))
    output.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>"
        + html.escape(title)
        + "</title></head><body><h1>"
        + html.escape(title)
        + "</h1><p>Multipass workflow-validation: "
        + "this report does not establish scientific performance.</p>"
        + "".join(charts)
        + "<h2>Measured evidence and qualification status</h2><pre>"
        + html.escape(text)
        + "</pre></body></html>"
    )
    return output


def render_report(
    manifest: CampaignManifest, evidence: RunEvidence | None, output: Path
) -> Path:
    """Produce standalone interactive HTML and the same numerical JSON."""
    return _write(summarize(manifest, evidence), output, f"One-shot {manifest.mode}")


def render_campaign(run_dir: Path, *, workflow_completed: bool | None = None) -> Path:
    """Require every frozen mode and repetition; report incomplete runs visibly."""
    manifests = []
    rows = []
    for directory in sorted(run_dir.glob("rep-*-*")):
        path = directory / "manifest.json"
        if not path.exists():
            continue
        manifest = CampaignManifest.model_validate_json(path.read_bytes())
        evidence_path = directory / "evidence.json"
        evidence = (
            RunEvidence.model_validate_json(evidence_path.read_bytes())
            if evidence_path.exists()
            else None
        )
        manifests.append(manifest)
        rows.append(summarize(manifest, evidence))
        render_report(manifest, evidence, directory / "report.html")
    frozen = []
    for manifest in manifests:
        identity = manifest.model_dump(
            exclude={"mode", "run_id", "anchor", "repetition"}
        )
        identity["parameters"].pop("nodeEndpoints", None)
        identity["modes"] = sorted(identity["modes"])
        frozen.append(identity)
    repetitions = (
        int(
            cast(dict[str, Any], manifests[0].parameters)
            .get("generator", {})
            .get("repetitions", 1)
        )
        if manifests
        else 0
    )
    modes = ("baseline", "oracle", "ewma")
    expected = repetitions * len(modes)
    required = {(rep, mode) for rep in range(repetitions) for mode in modes}
    observed = {(m.repetition, m.mode) for m in manifests}
    metadata_path = run_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    workflow_ok = (
        workflow_completed is True
        or (workflow_completed is None and metadata.get("status") == "passed")
    ) and metadata.get("status") not in {"failed", "cancelled", "running"}
    valid = bool(
        workflow_ok
        and expected
        and len(rows) == expected
        and observed == required
        and all(sorted(m.modes) == sorted(modes) for m in manifests)
        and all(row["conforming"] for row in rows)
        and all(value == frozen[0] for value in frozen)
    )
    between = {}
    for mode in modes if manifests else []:
        values = [
            row["throughputOriginalsPerSecond"]
            for row in rows
            if row["mode"] == mode
            and row["conforming"]
            and row["throughputOriginalsPerSecond"] is not None
        ]
        interval = None
        if len(values) > 1:
            rng = random.Random(manifests[0].seed)  # nosec B311: statistical bootstrap
            means = [
                statistics.mean(rng.choices(values, k=len(values))) for _ in range(1000)
            ]
            interval = [quantile(means, 0.025), quantile(means, 0.975)]
        between[mode] = {
            "sampleCount": len(values),
            "mean": statistics.mean(values) if values else None,
            "confidence95": interval,
            "method": "seeded-percentile-bootstrap",
        }
    return _write(
        {
            "validComparison": valid,
            "workflowCompleted": workflow_ok,
            "runs": rows,
            "betweenRepetitions": between,
        },
        run_dir / "comparison.html",
        "One-shot workflow-validation comparison",
    )
