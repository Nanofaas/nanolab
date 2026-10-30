"""Experiment 01 outcomes: the request ledger and its verdicts.

The historical matrix calls a cell done when its k6 summary and metrics snapshot
exist. Experiment 01 cannot: a control plane that runs out of its 64 MiB heap,
or never becomes ready, is a result of the experiment and produces neither file.
So each cell is receipted (`comparison.cells`), and a definitive outcome may be
a failure.

The ledger is rebuilt from the generator's own events, one send per request id
recorded immediately before `http.post`, and at most one terminal event. A
request belongs to the plateau it was SENT in, whenever it completes; one with no
terminal event when the generator stopped stays in its cohort as pending, never
silently dropped. Success within threshold is measured against every request the
cohort sent.

Three verdicts are kept apart and never folded into one another: the attempt's
outcome (was it measured at all), the step's load validity (did the generator
deliver the calendar), and the step's sustainability (only asked of a step whose
load was delivered). A 100% success rate at a rate the generator never sent is
not a result at that rate.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from nanolab.comparison.cells import (
    CELL_RESULT,
    EVENTS,
    SNAPSHOT,
    SUMMARY,
    attempt_dir,
    identity_sha256,
    sha256_file,
    write_json_atomically,
)
from nanolab.comparison.matrix import (
    EXPERIMENT_01_ORDER_POLICY,
    ComparisonCell,
)
from nanolab.config.experiment_01 import Experiment01Protocol, invocation_body
from nanolab.config.experiment_02 import Experiment02Protocol
from nanolab.images.control_plane_variants import experiment_01_catalog, runtime_class

# The Prometheus resolution the snapshot adapter queries at, and the window of
# every CPU rate in the catalogue. Recorded with each summary: a plateau
# statistic means nothing without the sampling that produced it.
QUERY_STEP_SECONDS = 5
CPU_RATE_WINDOW_SECONDS = 30

Failure = Literal["startup_failed", "oom", "transport"]


def classify_failure(pods: Mapping[str, Any], *, load_started: bool) -> Failure | None:
    """Classify a failed attempt from the namespace's pod listing.

    `oom` whenever a control-plane container was OOM-killed, before or during
    load. `startup_failed` when, before load, the control plane crashed or
    exited instead of becoming ready. `transport` when, before load, it is
    running and ready, so the failure was in reaching it. Anything else (an
    image pull, an error after load began, no control-plane pod at all) is
    unclassified and stops the matrix with its partial evidence.
    """
    statuses = [
        status
        for item in pods.get("items", [])
        if str(item.get("metadata", {}).get("name", "")).startswith(
            "nanofaas-control-plane"
        )
        for status in item.get("status", {}).get("containerStatuses", [])
        if status.get("name") == "control-plane"
    ]
    if not statuses:
        return None
    for status in statuses:
        for key in ("state", "lastState"):
            terminated = status.get(key, {}).get("terminated") or {}
            if terminated.get("reason") == "OOMKilled":
                return "oom"
    if load_started:
        return None
    for status in statuses:
        waiting = status.get("state", {}).get("waiting") or {}
        terminated = status.get("state", {}).get("terminated") or {}
        last = status.get("lastState", {}).get("terminated") or {}
        if waiting.get("reason") in {"CrashLoopBackOff", "Error"} or (
            terminated.get("exitCode", 0) != 0 or last.get("exitCode", 0) != 0
        ):
            return "startup_failed"
    if all(
        status.get("ready") and "running" in status.get("state", {})
        for status in statuses
    ):
        return "transport"
    return None


# ---------------------------------------------------------------- ledger


def read_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read generator events, tolerating a truncated last line.

    k6 writes console output one line per call, so an interrupted generator
    can leave half a line behind; it is counted, not parsed into a guess.
    Lines in k6's logfmt form carry the event as their `msg` field.
    """
    events: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        if not text.startswith("{") and 'msg="' in text:
            start = text.index('msg="') + 4
            try:
                text, _end = json.JSONDecoder().raw_decode(text[start:])
            except ValueError:
                malformed += 1
                continue
        try:
            event = json.loads(text)
        except ValueError:
            malformed += 1
            continue
        if isinstance(event, dict) and "t" in event:
            events.append(event)
        else:
            malformed += 1
    return events, malformed


def build_ledger(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Pair each request's send with at most one terminal event."""
    requests: dict[str, dict[str, Any]] = {}
    anomalies: list[str] = []
    origin: float | None = None
    for event in events:
        kind = event.get("t")
        if kind == "origin":
            origin = float(event["ts"])
            continue
        request_id = str(event.get("id", ""))
        if not request_id:
            anomalies.append(f"{kind} event without id")
            continue
        if kind == "send":
            if request_id in requests:
                anomalies.append(f"duplicate send {request_id}")
                continue
            requests[request_id] = {
                "phase": event["phase"],
                "sent": float(event["ts"]),
                "terminal": None,
            }
        elif kind in ("done", "timeout", "error"):
            request = requests.get(request_id)  # nosec B113: a dict, not HTTP
            if request is None:
                anomalies.append(f"terminal without send {request_id}")
            elif request["terminal"] is not None:
                anomalies.append(f"second terminal {request_id}")
            elif request["phase"] != event.get("phase"):
                anomalies.append(f"phase changed for {request_id}")
            else:
                request["terminal"] = dict(event)
        else:
            anomalies.append(f"unknown event {kind}")
    return {"origin": origin, "requests": requests, "anomalies": anomalies}


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _dropped(summary: Mapping[str, Any] | None, phase: str) -> float | None:
    if summary is None:
        return None
    metrics = summary.get("metrics", {})
    entry = metrics.get(f"dropped_iterations{{scenario:{phase}}}")
    if entry is None:
        # Present only when k6 dropped none in any scenario at all.
        return 0.0 if "dropped_iterations" not in metrics else None
    values = entry.get("values", entry)
    return float(values.get("count", 0.0))


def _series_points(
    snapshot: Mapping[str, Any] | None, name: str
) -> list[tuple[float, float]] | None:
    if snapshot is None:
        return None
    entry = snapshot.get("queries", {}).get(name)
    if not entry or entry.get("error") is not None:
        return None
    return [
        (datetime.fromisoformat(point["timestamp"]).timestamp(), float(point["value"]))
        for point in entry.get("points", [])
    ]


def _resource(
    points: list[tuple[float, float]] | None,
    start: float,
    end: float,
    *,
    rate_window: float = 0.0,
) -> dict[str, float | None] | None:
    """Summarise a series inside a phase, or say it cannot be summarised.

    A CPU rate point summarises the `rate_window` before it, so a point whose
    window crosses into the previous phase is left out of this one's summary.
    """
    if points is None:
        return None
    inside = [value for at, value in points if start + rate_window <= at < end]
    if not inside:
        return {"mean": None, "max": None, "samples": 0}
    return {
        "mean": statistics.fmean(inside),
        "max": max(inside),
        "samples": len(inside),
    }


_GC_COUNTERS = (
    "jvm_gc_collection_count",
    "jvm_gc_collection_time",
    "jvm_gc_young_count",
    "jvm_gc_full_count",
    "jvm_gc_young_time",
    "jvm_gc_full_time",
    "jvm_gc_pause_count",
    "jvm_gc_pause_sum",
    "jfr_vm_operation_count",
    "jfr_gc_operation_time",
)


def gc_phase(
    snapshot: Mapping[str, Any] | None, start: float, end: float
) -> dict[str, float | None]:
    """Keep missing counters distinct from zero using the shared reset reader."""
    from nanolab.metrics.interpretation import counter_delta

    values = {}
    for name in _GC_COUNTERS:
        entry = (snapshot or {}).get("queries", {}).get(name, {})
        points = [
            point
            for point in entry.get("points", [])
            if start <= datetime.fromisoformat(point["timestamp"]).timestamp() <= end
        ]
        values[name] = (
            counter_delta(points)
            if len(points) >= 2 and not entry.get("error")
            else None
        )
    return values


def summarize_phases(
    protocol: Experiment01Protocol | Experiment02Protocol,
    ledger: Mapping[str, Any],
    *,
    summary: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    malformed_events: int = 0,
    memory_series: str = "container_memory_bytes@control-plane",
    cpu_series: str = "container_cpu_cores_max@control-plane",
    service_mode: Literal["timely", "correct"] = "timely",
) -> list[dict[str, Any]]:
    """Reduce one measured attempt to a row per phase, with its verdicts.

    The resource series default to experiment 01's control plane; experiment 02
    names its deployment's instead.
    """
    criteria = protocol.criteria
    generator = protocol.generator
    origin = ledger.get("origin")
    requests = ledger["requests"]
    memory = _series_points(snapshot, memory_series)
    cpu = _series_points(snapshot, cpu_series)
    idle_reference: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for phase in protocol.phases():
        name = phase["name"]
        rate = float(phase["rate"])
        duration = float(phase["durationSeconds"])
        cohort = [r for r in requests.values() if r["phase"] == name]
        sent = len(cohort)
        terminals = [r["terminal"] for r in cohort if r["terminal"] is not None]
        completed = [t for t in terminals if t["t"] == "done"]
        correct = [t for t in completed if t.get("ok")]
        latency_population = correct if service_mode == "correct" else completed
        latencies = [float(t["dur"]) for t in latency_population]
        within = (
            [t for t in correct if float(t["dur"]) <= criteria.latency_threshold_ms]
            if criteria is not None
            else None
        )
        pending = sent - len(terminals)
        row: dict[str, Any] = {
            "phase": name,
            "offeredRate": rate,
            "scheduled": rate * duration,
            "sent": sent,
            "completed": len(completed),
            "correct": len(correct),
            "withinThreshold": len(within) if within is not None else None,
            "withinThresholdFraction": (
                len(within) / sent if sent and within is not None else None
            ),
            "timeouts": sum(1 for t in terminals if t["t"] == "timeout"),
            "errors": sum(1 for t in terminals if t["t"] == "error"),
            "rejected": sum(1 for t in completed if t.get("status") in (429, 503)),
            "pending": pending,
            "latencyPopulation": "completed responses of the send cohort",
            "p50Ms": _percentile(latencies, 0.50),
            "p95Ms": _percentile(latencies, 0.95),
            "p99Ms": _percentile(latencies, 0.99),
        }
        start = end = None
        if origin is not None:
            start = (origin + phase["startSeconds"] * 1000) / 1000
            end = start + duration
            working_set = _resource(memory, start, end)
            cores = _resource(
                cpu, start, end, rate_window=float(CPU_RATE_WINDOW_SECONDS)
            )
            if service_mode == "timely":
                row["gc"] = gc_phase(snapshot, start, end)
            row["workingSetBytes"] = working_set
            row["cpuCores"] = cores
            if name == "idle":
                idle_reference = {"memory": working_set, "cpu": cores}
            row["workingSetDeltaFromIdle"] = _delta(
                working_set, idle_reference.get("memory")
            )
            row["cpuDeltaFromIdle"] = _delta(cores, idle_reference.get("cpu"))
        if rate > 0:
            row["load"] = _load_validity(
                cohort,
                rate=rate,
                start=start,
                duration=duration,
                dropped=_dropped(summary, name),
                malformed_events=malformed_events,
                anomalies=len(ledger["anomalies"]),
                generator=generator if service_mode == "timely" else None,
            )
            if service_mode == "timely":
                row["sustainability"] = _sustainability(row, criteria)
            else:
                row["load"].pop("verdict")
                row["load"].pop("reasons")
        if service_mode == "correct":
            row.update(
                correctThroughput=len(correct) / duration,
                sentThroughput=sent / duration,
                successFraction=len(correct) / sent if sent else None,
                incorrect=sum(
                    1
                    for t in completed
                    if not t.get("ok") and t.get("status") not in (429, 503)
                ),
                latencyPopulation="correct responses of the send cohort",
            )
        rows.append(row)
    return rows


def _delta(
    value: Mapping[str, Any] | None, reference: Mapping[str, Any] | None
) -> float | None:
    if (
        not value
        or not reference
        or value.get("mean") is None
        or reference.get("mean") is None
    ):
        return None
    return float(value["mean"]) - float(reference["mean"])


def _load_validity(
    cohort: list[dict[str, Any]],
    *,
    rate: float,
    start: float | None,
    duration: float,
    dropped: float | None,
    malformed_events: int,
    anomalies: int,
    generator: Any,
) -> dict[str, Any]:
    """Decide whether the generator delivered this step's calendar.

    Measured against the calendar's origin even when the generator ran late:
    a late start is a deficit, not a shifted step. Windows are fixed slices of
    the step; the last one uses its actual length.
    """
    scheduled = rate * duration
    sent = len(cohort)
    result: dict[str, Any] = {
        "scheduled": scheduled,
        "sent": sent,
        "deviation": abs(sent - scheduled) / scheduled,
        "dropped": dropped,
        "droppedFraction": dropped / scheduled if dropped is not None else None,
        "startDelaySeconds": None,
        "worstWindowDeviation": None,
        "reasons": [],
    }
    reasons: list[str] = result["reasons"]
    if start is not None and cohort:
        result["startDelaySeconds"] = max(
            0.0, min(r["sent"] for r in cohort) / 1000 - start
        )
    if generator is None:
        reasons.append("no frozen generator thresholds (pilot)")
        result["verdict"] = "unverified"
        return result
    if start is None:
        reasons.append("calendar origin missing")
    else:
        worst = 0.0
        offset = 0.0
        while offset < duration:
            width = min(float(generator.window_seconds), duration - offset)
            low = (start + offset) * 1000
            high = low + width * 1000
            count = sum(1 for r in cohort if low <= r["sent"] < high)
            worst = max(worst, abs(count - rate * width) / (rate * width))
            offset += width
        result["worstWindowDeviation"] = worst
        if worst > generator.max_rate_deviation:
            reasons.append("window rate deviation above threshold")
    if result["deviation"] > generator.max_rate_deviation:
        reasons.append("step rate deviation above threshold")
    if dropped is None:
        reasons.append("dropped_iterations missing")
    elif dropped / scheduled > generator.max_dropped_fraction:
        reasons.append("dropped iterations above threshold")
    if result["startDelaySeconds"] is None:
        reasons.append("no request sent")
    elif result["startDelaySeconds"] > generator.max_start_delay_seconds:
        reasons.append("start delay above threshold")
    if malformed_events:
        reasons.append("truncated or malformed generator events")
    if anomalies:
        reasons.append("inconsistent request ledger")
    result["verdict"] = (
        "delivered" if not reasons else "not qualifiable: generator limit"
    )
    return result


def _sustainability(row: Mapping[str, Any], criteria: Any) -> str:
    if row["load"]["verdict"] != "delivered":
        return "not assessed"
    sent = row["sent"]
    if (
        row["withinThreshold"] / sent >= criteria.min_within_threshold_fraction
        and row["pending"] / sent <= criteria.max_pending_fraction
    ):
        return "sustained"
    return "not sustained"


def summarize_experiment_01_cell(
    cell_dir: Path, protocol: Experiment01Protocol
) -> dict[str, object]:
    """Summarise one cell from the attempt its receipt selects.

    A failed cell is reported with its outcome and diagnostics only; it has no
    phases to invent.
    """
    receipt = json.loads((cell_dir / CELL_RESULT).read_text(encoding="utf-8"))
    selected = attempt_dir(cell_dir, int(receipt["attempt"]))
    result: dict[str, object] = {
        "cell": receipt["cell"],
        "variant": receipt["variant"],
        "repetition": receipt["repetition"],
        "attempt": receipt["attempt"],
        "outcome": receipt["outcome"],
        "reason": receipt["reason"],
        "notProduced": receipt["notProduced"],
        "queryStepSeconds": QUERY_STEP_SECONDS,
        "cpuRateWindowSeconds": CPU_RATE_WINDOW_SECONDS,
    }
    if receipt["outcome"] != "measured":
        result["phases"] = []
        return result
    events, malformed = read_events(selected / EVENTS)
    ledger = build_ledger(events)
    summary = json.loads((selected / SUMMARY).read_text(encoding="utf-8"))
    snapshot = json.loads((selected / SNAPSHOT).read_text(encoding="utf-8"))
    result["malformedEvents"] = malformed
    result["anomalies"] = ledger["anomalies"]
    result["phases"] = summarize_phases(
        protocol,
        ledger,
        summary=summary,
        snapshot=snapshot,
        malformed_events=malformed,
    )
    return result


_CSV_FIELDS: tuple[str, ...] = (
    "variant",
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
)


def write_experiment_01_report(
    root: Path, cells: Iterable[ComparisonCell], protocol: Experiment01Protocol
) -> Path:
    """Write the machine-readable summary of every cell that has a receipt.

    Cells without a receipt are listed as incomplete, never as zero.
    """
    cells = tuple(cells)
    runtime_gc = (
        bool(cells)
        and next(iter(experiment_01_catalog(tuple(c.key for c in cells)))) == "jvm"
    )
    fields = (*_CSV_FIELDS, *_GC_COUNTERS) if runtime_gc else _CSV_FIELDS
    summaries: list[dict[str, object]] = []
    for cell in cells:
        if (cell.run_dir(root) / CELL_RESULT).is_file():
            summaries.append(summarize_experiment_01_cell(cell.run_dir(root), protocol))
        else:
            summaries.append(
                {
                    "cell": cell.label,
                    "variant": cell.variant.key,
                    "repetition": cell.repetition,
                    "outcome": "incomplete",
                    "phases": [],
                }
            )
    write_json_atomically(root / "experiment-01-summary.json", summaries)
    path = root / "experiment-01-summary.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for summary in summaries:
            phases: list[dict[str, Any]] = summary["phases"] or [{}]  # type: ignore[assignment]
            for phase in phases:
                row = {key: phase.get(key) for key in _CSV_FIELDS}
                row.update(
                    variant=summary["variant"],
                    repetition=summary["repetition"],
                    attempt=summary.get("attempt"),
                    outcome=summary["outcome"],
                    load=(phase.get("load") or {}).get("verdict"),
                    workingSetMeanBytes=(phase.get("workingSetBytes") or {}).get(
                        "mean"
                    ),
                    cpuMeanCores=(phase.get("cpuCores") or {}).get("mean"),
                )
                if runtime_gc:
                    row.update(
                        {
                            name: (phase.get("gc") or {}).get(name)
                            for name in _GC_COUNTERS
                        }
                    )
                writer.writerow(row)
    if runtime_gc:
        write_runtime_gc_html(root, cells, summaries, protocol)
    return path


def write_runtime_gc_html(
    root: Path,
    cells: tuple[ComparisonCell, ...],
    summaries: list[dict[str, Any]],
    protocol: Experiment01Protocol,
) -> None:
    """Reuse the comparison charts and phase shading for offline evidence."""
    from typing import cast

    import pandas as pd
    import plotly.io as pio

    from nanolab.tasks.loadtest.comparison_report import CellData, _over_time, _series
    from nanolab.tasks.loadtest.report import (
        _STYLE,
        ReportPhase,
        _shade_phases,
        _table_html,
    )

    metrics = {
        "container_memory_bytes@control-plane": "Control-plane working set (bytes)",
        "container_cpu_cores_max@control-plane": "Control-plane CPU (cores, 30 s)",
        "function_queue_depth": "Queue depth (requests)",
        "container_cpu_throttled_seconds@control-plane": "CPU throttling (seconds)",
    }
    cohort_metrics = {
        "offeredRate": ("Scheduled arrivals (requests/s)", None),
        "sent": ("Emitted requests (cohort requests/s)", "rate"),
        "completed": ("Completed HTTP responses (cohort requests/s)", "rate"),
        "p95Ms": ("Cohort p95 latency (ms)", None),
        "jvm_gc_young_time": ("MXBean young GC time per phase (seconds)", None),
        "jvm_gc_full_time": ("MXBean full GC time per phase (seconds)", None),
        "jfr_gc_operation_time": (
            "JFR collection-named operation time per phase (seconds)",
            None,
        ),
    }
    calendar = {phase["name"]: phase for phase in protocol.phases()}
    data = []
    labels = {cell.key: cell.variant.label for cell in cells}
    for cell, summary in zip(cells, summaries, strict=True):
        if summary["outcome"] != "measured":
            continue
        selected = attempt_dir(cell.run_dir(root), int(summary["attempt"]))
        snapshot = json.loads((selected / SNAPSHOT).read_text())
        events, _ = read_events(selected / EVENTS)
        origin = build_ledger(events).get("origin")
        series = {
            key: _series(snapshot, key)
            if origin is not None
            else pd.DataFrame(columns=["elapsed", "value"])
            for key in metrics
        }
        if origin is not None:
            offset = (
                datetime.fromisoformat(snapshot["start"]).timestamp() - origin / 1000
            )
            for frame in series.values():
                frame["elapsed"] += offset
        for field, (_title, reduction) in cohort_metrics.items():
            points = []
            for phase in summary["phases"]:
                window = calendar[phase["phase"]]
                value = (
                    (phase.get("gc") or {}).get(field)
                    if field in _GC_COUNTERS
                    else phase.get(field)
                )
                if value is not None and reduction == "rate":
                    value /= window["durationSeconds"]
                points.extend(
                    {"elapsed": elapsed, "value": value}
                    for elapsed in (
                        window["startSeconds"],
                        window["startSeconds"] + window["durationSeconds"],
                    )
                )
            series[field] = pd.DataFrame(points, columns=["elapsed", "value"])
        data.append(
            CellData(
                cell.key,
                cell.repetition,
                0,
                0,
                0,
                0,
                0,
                0,
                series,
            )
        )
    phases = tuple(
        ReportPhase(
            p["name"], p["startSeconds"], p["startSeconds"] + p["durationSeconds"]
        )
        for p in protocol.phases()
    )
    charts = []
    titles = metrics | {key: title for key, (title, _) in cohort_metrics.items()}
    for metric, title in titles.items():
        figure = _over_time(data, labels, metric, title, title)
        _shade_phases(figure, phases)
        charts.append(
            pio.to_html(
                figure,
                full_html=False,
                include_plotlyjs=cast(bool, "inline" if not charts else False),
            )
        )
    rows = [
        {
            "variant": summary["variant"],
            "run": summary["repetition"],
            "outcome": summary["outcome"],
            **phase,
        }
        for summary in summaries
        for phase in (summary["phases"] or [{}])
    ]
    table = _table_html(pd.json_normalize(rows), "Per-phase observations")
    (root / "experiment-01-report.html").write_text(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<style>{_STYLE}</style></head><body>"
        "<title>Runtime GC comparison</title><h1>Runtime GC comparison</h1>"
        "<p>Missing GC data is unavailable, not zero. "
        "MXBean and JFR values are distinct. "
        "Unaligned resource curves are omitted when the generator origin is missing. "
        "Timelines use idle start. Traffic and latency steps summarize send cohorts; "
        "they are phase values, including responses completed in later phases.</p>"
        + table
        + "".join(charts)
        + "</body></html>",
        encoding="utf-8",
    )


def experiment_01_identity(
    protocol: Experiment01Protocol,
    cells: tuple[ComparisonCell, ...],
    *,
    payload: bytes,
    script: Path,
    selected: tuple[str, ...],
) -> dict[str, object]:
    """Return what the manifest records about experiment 01 beyond the scenario.

    The effective order is part of the identity: a resume runs the remaining
    cells in it, and a changed order needs a new directory.
    """
    catalog = experiment_01_catalog(selected)
    runtime_gc = next(iter(catalog)) == "jvm"
    values = protocol.model_dump(mode="json", by_alias=True)
    return {
        "profile": "experiment-01",
        "orderPolicy": "runtime-gc-three-orders-v1"
        if runtime_gc
        else EXPERIMENT_01_ORDER_POLICY,
        "order": [cell.label for cell in cells],
        "protocol": values,
        "protocolSha256": identity_sha256(values),
        "payload": {
            "file": protocol.payload_file,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bodySha256": hashlib.sha256(invocation_body(payload)).hexdigest(),
            "bytes": len(payload),
        },
        "script": {"name": script.name, "sha256": sha256_file(script)},
        "builds": {
            key: {
                "label": variant.label,
                "modules": ["async-queue", "build-metadata", "k8s-deployment-provider"]
                if runtime_gc
                else list(variant.modules),
                "runtime": runtime_class(variant),
            }
            for key, variant in catalog.items()
            if key in selected
        },
        "subset": set(selected) != set(catalog),
    }
