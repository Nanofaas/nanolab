"""Measured workflow reports; incomplete observations never qualify a comparison."""

from __future__ import annotations

import html
import json
import math
import random
import re
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import plotly.graph_objects as go

from nanolab.one_shot.models import CampaignManifest
from nanolab.one_shot.vm_resources import (
    correlate_sample,
    read_samples,
    resource_clock_context,
)
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
    direct = {"planned": 0, "completed": 0, "errors": 0}
    if evidence:
        for cell in evidence.cells:
            if cell["origin"] in cloud:
                for key in direct:
                    direct[key] += int(cast(int, cell.get(key, 0)))
                continue
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
    result["outcomesByCell"] = evidence.model_dump()["cells"] if evidence else None
    edge_groups: dict[str, dict[str, dict[str, int]]] = {
        key: {} for key in ("function", "epoch", "origin")
    }
    if evidence:
        for cell in evidence.cells:
            if cell["origin"] in cloud:
                continue
            destinations = cast(dict[str, int], cell.get("destinations", {}))
            for dimension, groups in edge_groups.items():
                group = groups.setdefault(
                    str(cell[dimension]),
                    dict.fromkeys(
                        ("planned", "completed", "errors", "local", "peer", "cloud"), 0
                    ),
                )
                for field in ("planned", "completed", "errors"):
                    group[field] += int(cast(int, cell.get(field, 0)))
                for destination, count in destinations.items():
                    category = (
                        "cloud"
                        if destination in cloud
                        else "local"
                        if destination == cell["origin"]
                        else "peer"
                    )
                    group[category] += count
    for dimension, label in (
        ("function", "Function"),
        ("epoch", "Epoch"),
        ("origin", "Node"),
    ):
        result["edgeOutcomesBy" + label] = edge_groups[dimension] if evidence else None
    result["edgeOriginals"] = (
        {field: getattr(evidence, field) - direct[field] for field in direct}
        if evidence
        else None
    )
    if manifest.period_seconds == 360 and len(manifest.functions) == 5:
        result["analysisScope"] = "all-nine-synchronized-epochs; no steady-state claim"
    result["platformThroughputOriginalsPerSecond"] = result[
        "throughputOriginalsPerSecond"
    ]
    result["throughputOriginalsPerSecond"] = (
        (evidence.completed - direct["completed"]) / duration
        if evidence and duration
        else None
    )
    result["cloudDirect"] = direct if evidence else None
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
    _heterogeneous_details(manifest, evidence, result)
    return result


def _heterogeneous_details(manifest, evidence, result):
    """Expose phase demand and destinations without filtering outcomes."""
    parameters = cast(dict[str, Any], manifest.parameters)
    observations = cast(dict[str, Any], evidence.observations) if evidence else {}
    matrix = {}
    for cell in evidence.cells if evidence else []:
        origin = matrix.setdefault(cell["origin"], {})
        for destination, count in cell.get("destinations", {}).items():
            origin[destination] = origin.get(destination, 0) + count
    result["originDestination"] = matrix
    result["peerGraph"] = parameters.get("peerGraph")
    windows = observations.get("plannedWindows", [])
    phases = parameters.get("phaseIds") or [str(i) for i in range(len(windows))]
    requested = parameters.get("requestedWindows") or windows
    rows = []
    for epoch, window in enumerate(windows):
        for node, rates in window.items():
            profile = parameters.get("nodeProfiles", {}).get(node, {})
            profiles = {row["function"]: row for row in profile.get("functions", [])}
            limits = parameters.get("nodeFunctions", {}).get(node, {})
            for name, rate in rates.items():
                capacity = (parameters.get("capacities") or {}).get(node)
                reference = None
                records = [
                    record
                    for record in parameters.get("concurrentCapacities") or []
                    if record["node"] == node
                    and record["replicaVector"]
                    == parameters.get("staticReplicaVectors", {}).get(node)
                ]
                if records:
                    reference = max(
                        records, key=lambda record: sum(record["rates"].values())
                    )
                    capacity = reference["rates"][name]
                quota = limits.get(name, {})
                rows.append(
                    {
                        "epoch": epoch,
                        "phase": phases[epoch],
                        "node": node,
                        "function": name,
                        "requestedRate": requested[epoch][node][name],
                        "effectiveRate": rate,
                        "capacity": capacity,
                        "capacityScope": "measured-balanced-reference-vector"
                        if reference
                        else "scalar-legacy",
                        "replicaVector": reference["replicaVector"]
                        if reference
                        else None,
                        "loadToCapacity": rate / capacity if capacity else None,
                        "demandSeconds": profiles.get(name, {}).get("serviceSeconds"),
                        "cpuQuota": quota.get("cpu"),
                        "memoryMiB": quota.get("memoryMiB"),
                        "maxReplicas": quota.get("maxReplicas"),
                    }
                )
    result["phaseNodes"] = rows
    busy = {}
    for sample in observations.get("metricSamples", []):
        if sample["metrics"].get("nanofaas_runtime_active_handlers", 0) >= 1:
            key = (sample["node"], sample.get("function"), sample.get("replica"))
            busy[key] = busy.get(key, 0) + 1
    routing = result["routing"] or {}
    origins = (
        {cell["origin"] for cell in evidence.cells if cell["planned"] > 0}
        if evidence
        else set()
    )
    result["coverage"] = {
        "allOrigins": origins == set(manifest.nodes),
        "peerCompletions": routing.get("peer", 0),
        "verticalCompletions": routing.get("cloud", 0),
        "busyHandlerObserved": any(count >= 3 for count in busy.values()),
        "generatorHealthy": bool(evidence and not evidence.generator_deficit),
    }
    coverage = result["coverage"]
    result["coverageSatisfied"] = bool(
        manifest.mode != "baseline"
        and coverage["allOrigins"]
        and coverage["peerCompletions"]
        and coverage["verticalCompletions"]
        and coverage["busyHandlerObserved"]
        and coverage["generatorHealthy"]
    )
    result["coverageLimitations"] = (
        "busy handler samples are descriptive; pilot acceptance separately requires "
        "sustained physical saturation, cloud margin and native SDK proof"
    )


def _write(
    summary: dict[str, Any],
    output: Path,
    title: str,
    resource_charts: list[str] | None = None,
) -> Path:
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
    charts = [
        figure.to_html(full_html=False, include_plotlyjs=True),
        *(resource_charts or []),
    ]
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
        matrix = row.get("originDestination", {})
        if matrix:
            nodes = sorted(
                set(matrix) | {d for counts in matrix.values() for d in counts}
            )
            figure = go.Figure(
                go.Heatmap(
                    x=nodes,
                    y=nodes,
                    z=[
                        [
                            matrix.get(origin, {}).get(destination, 0)
                            for destination in nodes
                        ]
                        for origin in nodes
                    ],
                    colorbar={"title": "Completions"},
                )
            )
            figure.update_layout(
                title=f"{row['mode']}: origin/destination (includes cloud direct)"
            )
            charts.append(figure.to_html(full_html=False, include_plotlyjs=False))
        graph = row.get("peerGraph")
        if graph:
            nodes = sorted(graph)
            positions = {
                node: (
                    math.cos(2 * math.pi * i / len(nodes)),
                    math.sin(2 * math.pi * i / len(nodes)),
                )
                for i, node in enumerate(nodes)
            }
            figure = go.Figure()
            for node, neighbors in graph.items():
                for neighbor in neighbors:
                    if node < neighbor and neighbor in positions:
                        figure.add_scatter(
                            x=[positions[node][0], positions[neighbor][0]],
                            y=[positions[node][1], positions[neighbor][1]],
                            mode="lines",
                            showlegend=False,
                        )
            figure.add_scatter(
                x=[positions[node][0] for node in nodes],
                y=[positions[node][1] for node in nodes],
                text=nodes,
                mode="markers+text",
                showlegend=False,
            )
            figure.update_layout(
                title=("Horizontal peer graph; every edge also reaches terminal cloud")
            )
            charts.append(figure.to_html(full_html=False, include_plotlyjs=False))
        phase_rows = row.get("phaseNodes", [])
        if phase_rows:
            figure = go.Figure()
            for node, name in sorted(
                {(cell["node"], cell["function"]) for cell in phase_rows}
            ):
                cells = [
                    cell
                    for cell in phase_rows
                    if cell["node"] == node and cell["function"] == name
                ]
                figure.add_scatter(
                    x=[cell["epoch"] for cell in cells],
                    y=[cell["loadToCapacity"] for cell in cells],
                    text=[cell["phase"] for cell in cells],
                    name=f"{node}/{name}",
                )
            qualifier = (
                "reference "
                if any(
                    cell.get("capacityScope") == "measured-balanced-reference-vector"
                    for cell in phase_rows
                )
                else ""
            )
            figure.update_layout(
                title=f"{row['mode']}: effective load / measured {qualifier}capacity"
            )
            charts.append(figure.to_html(full_html=False, include_plotlyjs=False))
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
        + "</h1><p>Multipass on a shared physical host: "
        + "a single run does not establish a paired campaign result.</p>"
        + "".join(charts)
        + "<h2>Measured evidence and qualification status</h2><pre>"
        + html.escape(text)
        + "</pre></body></html>"
    )
    return output


def assignment_observations(directory: Path) -> dict:
    """Keep node-local quantities separate from actual request observations."""
    cells = []
    path = directory / "epochs.jsonl"
    epochs, malformed_epochs = read_samples(path)
    if path.exists():
        for epoch in epochs:
            for node, observed in epoch.get("nodes", {}).items():
                result = observed.get("result") or {}
                plan = result.get("plan") or {}
                for function, allocation in plan.get("functions", {}).items():
                    diagnostics = [
                        d
                        for d in result.get("diagnostics", [])
                        if d["function"] == function
                    ]
                    cells.append(
                        {
                            "epoch": epoch["epoch"],
                            "node": node,
                            "function": function,
                            "status": result.get("status"),
                            "executionUsable": observed.get("executionUsable"),
                            "fullyPrepared": observed.get("fullyPrepared"),
                            "measurementComplete": observed.get("measurementComplete"),
                            "assignments": len(diagnostics),
                            "negotiatedQuantity": sum(
                                d["negotiatedQuantity"] for d in diagnostics
                            ),
                            "confirmedQuantity": sum(
                                d["confirmedQuantity"] for d in diagnostics
                            ),
                            "unconfirmedQuantity": sum(
                                d["negotiatedQuantity"] - d["confirmedQuantity"]
                                for d in diagnostics
                            ),
                            "plannedCloudRate": allocation.get("cloudRate"),
                            "diagnostics": diagnostics,
                        }
                    )
    unused, malformed_unused = read_samples(directory / "assignment-observations.jsonl")
    return {
        "cells": cells,
        "unusedReservations": unused,
        "malformedLines": {
            "epochs.jsonl": malformed_epochs,
            "assignment-observations.jsonl": malformed_unused,
        },
        "units": (
            "assignment quantities in flow quanta; plannedCloudRate in requests/s; "
            "actual requests in returnedResponses"
        ),
        "perspective": (
            "node-local observations; do not sum buyer and seller "
            "as distinct assignments"
        ),
    }


def render_report(
    manifest: CampaignManifest, evidence: RunEvidence | None, output: Path
) -> Path:
    """Produce standalone interactive HTML and the same numerical JSON."""
    summary = summarize(manifest, evidence)
    resource_charts = []
    summary["observationOnly"] = manifest.parameters.get("observationOnly") is True
    summary["operationalOutcome"] = (
        json.loads((output.parent / "result.json").read_text())
        if (output.parent / "result.json").exists()
        else {"state": "not-finalized"}
    )
    summary["assignmentObservations"] = assignment_observations(output.parent)
    schedule_path = output.parent / "schedule.json"
    if schedule_path.exists():
        from nanolab.one_shot.returned_errors import summarize_responses
        from nanolab.tasks.one_shot.experiment import generator_rows

        summary["returnedResponses"] = summarize_responses(
            json.loads(schedule_path.read_bytes()),
            generator_rows(output.parent / "generator.jsonl"),
            mode=manifest.mode,
        )
        totals = summary["returnedResponses"]["phases"]["campaign"]["total"]
        resource_charts.append(
            "<h2>Returned errors</h2><p>"
            + html.escape(
                f"{totals['returnedErrors']} returned errors / "
                f"{totals['planned']} planned / {totals['emitted']} emitted originals; "
                f"rejects: {totals['reject']}, HTTP errors: {totals['httpError']}, "
                f"application errors: {totals['applicationError']}, "
                f"invalid responses: {totals['invalidResponse']}, "
                f"HTTP timeout responses: {totals['timeoutResponse']}, "
                f"unresolved responses: {totals['unresolved']}, "
                f"missing results: {totals['missingResult']}. "
                "Warmup and per-node/function/epoch counts and percentages "
                "appear below; "
                "full responses remain in generator.jsonl."
            )
            + "</p>"
        )
    if manifest.parameters.get("vmTelemetry") or any(
        (output.parent / "vm-resources").glob("*.jsonl")
    ):
        from nanolab.one_shot.vm_resources import summarize_resources

        generator = cast(dict, manifest.parameters.get("generator") or {})
        coverage = summarize_resources(
            output.parent,
            anchor=datetime.fromisoformat(manifest.anchor),
            warmup=int(generator.get("warmupSeconds", 720)),
            period=int(manifest.period_seconds),
            epochs=int(
                cast(dict, manifest.parameters.get("logicalSchedule") or {}).get(
                    "epochCount", 9
                )
            ),
            nodes=manifest.nodes,
            mode=manifest.mode,
            functions=manifest.functions,
            run_id=manifest.run_id,
        )
        summary["vmResources"] = coverage
        resource_charts.extend(resource_figures(output.parent, manifest))
    return _write(summary, output, f"One-shot {manifest.mode}", resource_charts)


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
        report = render_report(manifest, evidence, directory / "report.html")
        rows.append(json.loads(report.with_suffix(".json").read_text()))
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
        (
            "One-shot scientific block (paired main analysis separate)"
            if manifests and manifests[0].purpose == "scientific-experiment"
            else "One-shot workflow-validation comparison"
        ),
    )


def render_paired_campaign(blocks, output: Path, *, seed: int, protocol=None) -> Path:
    """Write the same paired numerical summary and standalone temporal figure."""
    from nanolab.one_shot.campaign import paired_summary

    summary = paired_summary(blocks, seed=seed, protocol=protocol)
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, indent=2, allow_nan=False)
    output.with_suffix(".json").write_text(text + "\n")
    figure = go.Figure(
        go.Scatter(
            x=summary["serialDiagnostics"]["blockOrder"],
            y=summary["serialDiagnostics"]["primarySequence"],
            mode="lines+markers",
            name="EWMA minus baseline cloud share",
        )
    )
    figure.update_layout(title="Paired block contrasts in registered execution order")
    output.write_text(
        "<!doctype html><html><meta charset='utf-8'>"
        "<title>One-shot paired campaign</title>"
        "<h1>One-shot paired campaign</h1><p>Shared host: inference is conditional "
        "on independent blocks. Pilot data are excluded.</p>"
        + figure.to_html(full_html=False, include_plotlyjs=True)
        + "<pre>"
        + html.escape(text)
        + "</pre></html>"
    )
    return output


def resource_figures(directory: Path, manifest: CampaignManifest) -> list[str]:
    """Show full node CPU/RAM/swap and individual replica counters, with gaps."""
    charts = []
    anchor = datetime.fromisoformat(manifest.anchor)
    clock_context = resource_clock_context(directory)
    generator = cast(dict, manifest.parameters.get("generator") or {})
    time_axis = (
        "Host monotonic time projected on frozen UTC anchor" if clock_context else "UTC"
    )
    for node in manifest.nodes:
        rows, _malformed = read_samples(directory / "vm-resources" / (node + ".jsonl"))
        # Insert missing timeline seconds explicitly: Plotly must not connect a gap.
        timed = []
        previous = None
        for row in rows:
            try:
                if clock_context:
                    row = correlate_sample(
                        row,
                        anchor=anchor,
                        warmup=int(generator.get("warmupSeconds", 720)),
                        period=int(manifest.period_seconds),
                        epochs=int(
                            cast(
                                dict, manifest.parameters.get("logicalSchedule") or {}
                            ).get("epochCount", 9)
                        ),
                        mode=manifest.mode,
                        functions=manifest.functions,
                        run_id=manifest.run_id,
                        **clock_context,
                    )
                    offset = row["timeFromAnchorSeconds"]
                    timestamp = (
                        anchor + timedelta(seconds=offset)
                        if offset is not None
                        else None
                    )
                else:
                    timestamp = datetime.fromisoformat(row["timestampUtc"])
            except (KeyError, ValueError, TypeError):
                continue
            row = {
                **row,
                "plotTimestampUtc": timestamp.isoformat() if timestamp else None,
            }
            if (
                previous is not None
                and timestamp is not None
                and (timestamp - previous).total_seconds() > 1.5
            ):
                timed.append({"plotTimestampUtc": row["plotTimestampUtc"]})
            timed.append(row)
            previous = timestamp
        x = [row["plotTimestampUtc"] for row in timed]
        raw_utc = [row.get("timestampUtc") for row in timed]
        cpu = go.Figure()
        for name, field in [
            ("VM busy / vCPU capacity", "cpuUtilization"),
            ("I/O wait", "cpuIowaitFraction"),
            ("Steal", "cpuStealFraction"),
        ]:
            cpu.add_scatter(
                name=name,
                x=x,
                y=[row.get(field) for row in timed],
                connectgaps=False,
                customdata=raw_utc,
                hovertemplate=(
                    "%{x}: %{y}<br>VM UTC: %{customdata}<extra>%{fullData.name}</extra>"
                ),
            )
        cpu.update_layout(
            title="Whole VM CPU: " + node,
            yaxis_title="Fraction of vCPU capacity",
            xaxis_title=time_axis,
        )
        memory = go.Figure()
        for field in [
            "memoryTotalBytes",
            "memoryAvailableBytes",
            "memoryUsedBytes",
            "swapTotalBytes",
            "swapUsedBytes",
        ]:
            memory.add_scatter(
                name=field, x=x, y=[row.get(field) for row in timed], connectgaps=False
            )
        memory.update_layout(
            title="Whole VM memory and swap: " + node,
            yaxis_title="Bytes",
            xaxis_title=time_axis,
        )
        figures = [cpu, memory]
        identities = {
            (container["containerId"], container.get("incarnation")): container
            for row in rows
            for container in row.get("containers", [])
        }
        for field, title, unit in [
            ("cpuUsedCores", "Container CPU", "Used CPU cores"),
            ("memoryCurrentBytes", "Container memory", "Bytes"),
            (
                "throttledUsecDelta",
                "Container throttling",
                "Throttled microseconds per sample",
            ),
        ]:
            figure = go.Figure()
            for identity, container in identities.items():
                values = []
                for row in timed:
                    match = next(
                        (
                            c
                            for c in row.get("containers", [])
                            if (c["containerId"], c.get("incarnation")) == identity
                        ),
                        None,
                    )
                    values.append(match.get(field) if match else None)
                label = ":".join(
                    str(value)
                    for value in [
                        container["role"],
                        container.get("function"),
                        container.get("replica"),
                        identity[0][:12],
                        identity[1],
                    ]
                )
                figure.add_scatter(name=label, x=x, y=values, connectgaps=False)
            figure.update_layout(
                title=title + ": " + node, yaxis_title=unit, xaxis_title=time_axis
            )
            figures.append(figure)
        for figure in figures:
            for epoch in range(10):
                figure.add_vline(
                    x=(
                        anchor + timedelta(seconds=epoch * manifest.period_seconds)
                    ).isoformat(),
                    line_width=1,
                    line_dash="dot",
                )
            charts.append(figure.to_html(full_html=False, include_plotlyjs=False))
    return charts


def finalize_operational_outcome(directory: Path, outcome: dict) -> None:
    """Update each valid report independently, preserving scientific data/charts."""
    summary_path, report_path = directory / "report.json", directory / "report.html"
    summary = embedded = document = block = None
    errors = []
    try:
        if summary_path.exists():
            summary = json.loads(summary_path.read_text())
            if not isinstance(summary, dict):
                raise ValueError("report JSON summary is not an object")
    except (OSError, ValueError) as error:
        summary = None
        errors.append((summary_path, error))
    try:
        if report_path.exists():
            # Preserve line endings and every byte outside the generated summary.
            document = report_path.read_bytes().decode("utf-8")
            blocks = list(re.finditer(r"<pre>(.*?)</pre>", document, re.DOTALL))
            if len(blocks) != 1:
                raise ValueError("report HTML summary is missing or ambiguous")
            block = blocks[0]
            embedded = json.loads(html.unescape(block.group(1)))
            if not isinstance(embedded, dict):
                raise ValueError("report HTML summary is not an object")
    except (OSError, ValueError) as error:
        embedded = None
        errors.append((report_path, error))
    if summary is not None and embedded is not None:
        # A partial terminal write can change only operationalOutcome. Scientific
        # disagreement is never repaired from a different diagnostic output.
        scientific_json = {
            k: v for k, v in summary.items() if k != "operationalOutcome"
        }
        scientific_html = {
            k: v for k, v in embedded.items() if k != "operationalOutcome"
        }
        if json.dumps(scientific_json, sort_keys=True, allow_nan=False) != json.dumps(
            scientific_html, sort_keys=True, allow_nan=False
        ):
            raise ValueError("report summary differs from saved evidence")
    if embedded is not None:
        if document is None or block is None:
            raise ValueError("report HTML summary is missing")
        try:
            embedded["operationalOutcome"] = dict(outcome)
            current = html.escape(json.dumps(embedded, indent=2, allow_nan=False))
            report_path.write_text(
                document[: block.start(1)] + current + document[block.end(1) :],
                encoding="utf-8",
                newline="",
            )
        except (OSError, ValueError) as error:
            errors.append((report_path, error))
    if summary is not None:
        try:
            summary["operationalOutcome"] = dict(outcome)
            summary_path.write_text(
                json.dumps(summary, indent=2, allow_nan=False) + "\n"
            )
        except (OSError, ValueError) as error:
            errors.append((summary_path, error))
    if errors:
        primary = errors[0][1]
        for path, error in errors:
            primary.add_note("report update " + str(path) + ": " + str(error))
        raise primary
