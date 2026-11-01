"""Rebuild B4 outcomes from archived requests, generator and node evidence."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from nanolab.assets.diagnostics.idle_reactivation_probe import equal, load_inputs
from nanolab.tasks.idle_reactivation import parse_observation
from nanolab.tasks.loadtest.offload_conservation import _sum_metric

_ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    AttributeError,
    OverflowError,
)
_COUNTERS = (
    "function_success_total",
    "function_retry_total",
    "nanofaas_offload_total",
    "nanofaas_offload_failure_total",
)


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a measurement")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("measurement must be finite and nonnegative")
    return number


def _count(value: Any) -> int:
    number = _number(value)
    if number != int(number):
        raise ValueError("count must be an integer")
    return int(number)


def _invalid_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON constant: {value}")


def _read(root: Path, name: str, errors: list[str]) -> dict[str, Any]:
    try:
        data = json.loads((root / name).read_text(), parse_constant=_invalid_constant)
        if not isinstance(data, dict):
            raise ValueError("expected object")
        return data
    except _ERRORS as error:
        errors.append(f"{name}: {error}")
        return {}


def _events(root: Path, errors: list[str]) -> list[dict[str, Any]]:
    records = []
    try:
        lines = (root / "k6-events.jsonl").read_text().splitlines()
    except OSError as error:
        raise ValueError(f"k6-events.jsonl: {error}") from error
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            if line.lstrip().startswith("{"):
                record = json.loads(line)
            else:
                match = re.search(r'\bmsg=("(?:[^"\\]|\\.)*")', line)
                if not match:
                    # k6 progress and diagnostics are retained in the source log.
                    if "source=console" in line:
                        raise ValueError("unreadable console record")
                    continue
                message = json.loads(match[1])
                if not message.startswith("{"):
                    continue
                record = json.loads(message)
            if not isinstance(record, dict):
                raise ValueError("expected object")
            if record.get("t") == "b4":
                records.append(record)
            elif record.get("phase") in ("qualification", "warmup", "measurement"):
                raise ValueError("request record lacks B4 identity")
        except _ERRORS as error:
            errors.append(f"k6-events.jsonl:{index}: {error}")
    return records


def _latencies(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(r["duration_ms"] for r in rows if r["outcome"] == "success")
    return {
        "count": len(values),
        "method": "nearest rank",
        **{
            f"p{percent}": values[math.ceil(percent / 100 * len(values)) - 1]
            if values
            else None
            for percent in (50, 95, 99)
        },
    }


def _requests(
    events: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    mode: str,
    cloud_url: str,
    errors: list[str],
) -> list[dict[str, Any]]:
    rows, seen = [], set()
    for index, event in enumerate(events):
        try:
            phase, target = event["phase"], event["target"]
            iteration = _count(event["iteration"])
            if phase not in ("qualification", "warmup", "measurement"):
                raise ValueError("unknown phase")
            if target not in ("edge", "cloud"):
                raise ValueError("unknown target")
            if phase != "qualification" and target != (
                "cloud" if mode == "cloud" else "edge"
            ):
                raise ValueError("mixed treatment targets")
            identity = (phase, target if phase == "qualification" else "", iteration)
            if identity in seen:
                raise ValueError("duplicate iteration")
            seen.add(identity)
            fixture = fixtures[iteration % len(fixtures)]
            if event["case_name"] != fixture["name"]:
                raise ValueError("incorrect fixture rotation")
            status, error_code = _count(event["status"]), _count(event["error_code"])
            if status > 599 or 0 < status < 100:
                raise ValueError("invalid HTTP status")
            duration, http_duration, timestamp = (
                _number(event[key]) for key in ("duration_ms", "http_duration_ms", "ts")
            )
            if not isinstance(event["body"], str):
                raise ValueError("response body must be preserved as text")
            marker = event["offloaded"]
            if marker is not None and not isinstance(marker, str):
                raise ValueError("offload marker must be header text or null")
            path = "local"
            if marker:
                if marker.rstrip("/") == cloud_url.rstrip("/"):
                    path = "offloaded"
                else:
                    path = "unknown"
                    errors.append(
                        f"request {index}: unexpected offload target {marker!r}"
                    )
            if path == "offloaded" and (mode == "disabled" or target == "cloud"):
                errors.append(f"request {index}: forbidden offload")
            if status == 200:
                try:
                    body = json.loads(event["body"], parse_constant=_invalid_constant)
                    valid = (
                        isinstance(body, dict)
                        and body.get("status") == "success"
                        and equal(body.get("output"), fixture["expected"])
                    )
                except ValueError:
                    valid = False
                outcome = (
                    ("success" if error_code == 0 else "transport_error")
                    if valid
                    else "invalid_body"
                )
                if not valid:
                    errors.append(f"request {index}: HTTP 200 violates exact oracle")
            elif status in (429, 503):
                outcome = "refused"
            elif status == 0 or error_code:
                outcome = "transport_error"
            else:
                outcome = "http_error"
            if phase == "qualification" and outcome != "success":
                errors.append(f"request {index}: qualification failed")
            rows.append(
                dict(
                    event,
                    outcome=outcome,
                    observed_path=path,
                    duration_ms=duration,
                    http_duration_ms=http_duration,
                    ts=timestamp,
                    iteration=iteration,
                )
            )
        except _ERRORS as error:
            errors.append(f"request {index}: {error}")
    for target in ("edge", "cloud"):
        indices = [
            r["iteration"]
            for r in rows
            if r["phase"] == "qualification" and r["target"] == target
        ]
        if sorted(indices) != list(range(4)):
            errors.append(f"qualification {target}: expected iterations 0..3")
    return rows


def _inventory(observation: dict[str, Any], function: str) -> dict[str, Any]:
    state = parse_observation(
        observation["inventory"], function, _number(observation["captured_at"])
    )
    if len(state["control_plane"]) != 1 or not state["pods"]:
        raise ValueError("missing or ambiguous control plane/function pods")
    identities = {}
    for pod in state["control_plane"] + state["pods"]:
        if not pod["uid"] or pod["restarts"] is None or pod["deleting"]:
            raise ValueError("missing pod identity, image or restart evidence")
        identities[pod["name"]] = {
            key: pod[key] for key in ("uid", "image_id", "restarts")
        }
    if not state["control_plane"][0]["image_id"]:
        raise ValueError("control-plane image identity absent")
    return identities


def _diagnostics(
    root: Path, function: str, mode: str, errors: list[str]
) -> dict[str, Any]:
    result = {}
    for node in ("stack", "cloud"):
        before, after = [
            _read(root, f"observations/{node}-{phase}.json", errors)
            for phase in ("before", "after")
        ]
        diagnostic: dict[str, Any] = {
            "deltas": dict.fromkeys(_COUNTERS),
            "counter_observations": {},
            "inventory_stable": False,
            "history_available": "history" in after,
            "window": "whole run including qualification and warmup",
        }
        result[node] = diagnostic
        stable = False
        try:
            first, last = _inventory(before, function), _inventory(after, function)
            diagnostic["identities"] = {"before": first, "after": last}
            if first.keys() != last.keys():
                raise ValueError("pod inventory changed")
            for name, initial in first.items():
                final = last[name]
                if (
                    initial["uid"] != final["uid"]
                    or initial["restarts"] != final["restarts"]
                    or not final["image_id"]
                    or initial["image_id"] not in (None, "", final["image_id"])
                ):
                    raise ValueError("pod UID, image or restart count changed")
                if not initial["image_id"]:
                    diagnostic.setdefault("limitations", []).append(
                        f"{name}: function image identity only available after startup"
                    )
            if _number(after["captured_at"]) <= _number(before["captured_at"]):
                raise ValueError("observation times are unordered")
            diagnostic["inventory_stable"] = True
            uptime = [
                _sum_metric(s["metrics"], "process_uptime_seconds", {}, required=True)
                for s in (before, after)
            ]
            if uptime[1] < uptime[0]:
                raise ValueError("control-plane uptime reset")
            diagnostic["uptime_seconds"] = uptime
            stable = True
        except _ERRORS as error:
            errors.append(f"{node}: {error}")
        for name in _COUNTERS:
            values = []
            for phase, snapshot in (("before", before), ("after", after)):
                try:
                    values.append(
                        _sum_metric(
                            snapshot["metrics"],
                            name,
                            {"function": function},
                            required=True,
                        )
                    )
                except _ERRORS as error:
                    values.append(None)
                    message = f"{node} {phase}: {error}"
                    if name.startswith("nanofaas_offload"):
                        diagnostic.setdefault("unavailable", []).append(message)
                    else:
                        errors.append(message)
            low, high = values
            diagnostic["counter_observations"][name] = {"before": low, "after": high}
            if (
                name == "nanofaas_offload_total"
                and high is not None
                and high > 0
                and (node == "cloud" or mode == "disabled")
            ):
                errors.append(f"{node}: forbidden offload count {high}")
            if low is None or high is None:
                continue
            if high < low:
                errors.append(f"{node}: {name} reset")
            elif stable:
                delta = high - low
                diagnostic["deltas"][name] = delta
                if name == "function_retry_total" and delta != 0:
                    errors.append(f"{node}: forbidden retry count {delta}")
    return result


def analyze_experiment(run_dir: Path) -> dict[str, Any]:
    """Persist an honest verdict even when an interrupted run lacks evidence."""
    run_dir = Path(run_dir)
    errors: list[str] = []
    report: dict[str, Any] = {
        "integrity_passed": False,
        "errors": errors,
        "latency_boundary": "client call to complete response body",
        "latency_population": "valid successful measurement completions only",
        "scheduling_boundary": (
            "planned = rate * duration; scheduled = accounted iterations + drops. "
            "Fixed k6 boundary tolerance: at most one extra scheduled iteration per "
            "phase, with explicit phase drop evidence and complete ledger. "
            "Missing requests are never absorbed by this tolerance."
        ),
        "goodput_boundary": (
            "valid completions of the scheduled measurement cohort, including drain "
            "completions, divided by scheduled-window seconds; "
            "not observed wall-clock throughput"
        ),
        "offload_boundary": "observed response marker; not physical exactly-once proof",
        "counter_boundary": "edge success includes cloud success; never add them",
        "memory": {"available": False, "reason": "raw history retained; no RAM claim"},
    }
    rows: list[dict[str, Any]] = []
    try:
        protocol = _read(run_dir, "protocol.json", errors)
        report["protocol"] = protocol
        config = protocol["scenario"]["offloadExperiment"]
        mode = config["mode"]
        if mode not in ("disabled", "pressure", "cloud"):
            raise ValueError("unknown experiment mode")
        rate, duration, warmup = (
            _count(config[key]) for key in ("rate", "durationSeconds", "warmupSeconds")
        )
        if rate == 0 or duration == 0 or warmup == 0:
            raise ValueError("rate and phase durations must be positive")
        planned = {"warmup": warmup * 20, "measurement": duration * rate}
        report["mode"] = mode
        fixtures = load_inputs(
            run_dir / "inputs/performance-medium.json",
            run_dir / "inputs/runtime-contract.json",
        )
        cloud_url = protocol["cloud_url"]
        if not isinstance(cloud_url, str) or not cloud_url:
            raise ValueError("cloud URL is missing")
        rows = _requests(_events(run_dir, errors), fixtures, mode, cloud_url, errors)
        for phase, count in planned.items():
            indices = sorted(r["iteration"] for r in rows if r["phase"] == phase)
            if indices != list(range(len(indices))) or len(indices) > count + 1:
                errors.append(f"{phase}: noncontiguous or excessive iteration sequence")
        measured = [r for r in rows if r["phase"] == "measurement"]
        outcomes = Counter(r["outcome"] for r in measured)
        report["outcomes"] = {
            key: outcomes[key]
            for key in (
                "success",
                "refused",
                "transport_error",
                "http_error",
                "invalid_body",
            )
        }
        report["refused_by_status"] = {
            str(status): sum(r["status"] == status for r in measured)
            for status in (429, 503)
        }
        report["goodput_per_second"] = outcomes["success"] / duration
        report["completion_probability_scheduled"] = None
        report["completion_probability_emitted"] = (
            outcomes["success"] / len(measured) if measured else None
        )
        report["latency_ms"] = {
            path: _latencies([r for r in measured if r["observed_path"] == path])
            for path in ("local", "offloaded", "unknown")
        }
        report["latency_ms"]["overall"] = _latencies(measured)
        summary = _read(run_dir, "k6-summary.json", errors)
        report["counts"] = {
            phase: {
                "planned": count,
                "scheduled": None,
                "boundary_extra": None,
                "emitted": sum(r["phase"] == phase for r in rows),
                "dropped": None,
                "missing": None,
            }
            for phase, count in planned.items()
        }
        total = {
            "planned": sum(planned.values()),
            "scheduled": None,
            "boundary_extra": None,
            "emitted": sum(r["phase"] != "qualification" for r in rows),
            "dropped": None,
            "missing": None,
            "k6_iterations": None,
        }
        report["counts"]["total"] = total
        try:
            metrics = summary["metrics"]
            iterations = _count(metrics["iterations"]["count"])
            # k6 omits this built-in metric when no iteration was dropped.
            dropped = _count(metrics.get("dropped_iterations", {"count": 0})["count"])
            total.update(
                k6_iterations=iterations,
                dropped=dropped,
                scheduled=iterations + dropped,
                boundary_extra=max(0, iterations + dropped - total["planned"]),
                missing=max(
                    0,
                    iterations - total["emitted"],
                    total["planned"] - iterations - dropped,
                ),
                missing_ledger_records=max(0, iterations - total["emitted"]),
            )
            ledger_complete = iterations == total["emitted"]
            if not ledger_complete:
                errors.append("k6 iterations do not equal complete emitted ledger")
            for phase in planned:
                count = report["counts"][phase]
                tag = f"{{scenario:{phase}}}"
                phase_drop = metrics.get("dropped_iterations" + tag)
                phase_iterations = metrics.get("iterations" + tag)
                completed = count["emitted"] if ledger_complete else None
                if phase_iterations is not None:
                    completed = _count(phase_iterations["count"])
                    count["missing"] = max(0, completed - count["emitted"])
                    if completed != count["emitted"]:
                        errors.append(f"{phase}: k6 iterations differ from ledger")
                if phase_drop is not None:
                    count["dropped"] = _count(phase_drop["count"])
                    count["missing"] = max(
                        count["missing"] or 0,
                        count["planned"] - count["dropped"] - count["emitted"],
                    )
                elif (
                    ledger_complete
                    and iterations + dropped == total["planned"]
                    and count["emitted"] <= count["planned"]
                ):
                    count.update(dropped=count["planned"] - count["emitted"], missing=0)
                    count["drop_attribution"] = (
                        "inferred from reconciled complete ledger"
                    )
                else:
                    errors.append(f"{phase}: phase drop evidence unavailable")
                if completed is not None and count["dropped"] is not None:
                    scheduled = completed + count["dropped"]
                    count["scheduled"] = scheduled
                    count["boundary_extra"] = max(0, scheduled - count["planned"])
                    if not count["planned"] <= scheduled <= count["planned"] + 1:
                        errors.append(
                            f"{phase}: scheduled count outside nominal/+1 boundary"
                        )
            phase_counts = [report["counts"][phase] for phase in planned]
            if (
                all(count["dropped"] is not None for count in phase_counts)
                and sum(count["dropped"] for count in phase_counts) != dropped
            ):
                errors.append("phase drops do not equal total dropped iterations")
            scheduled = report["counts"]["measurement"]["scheduled"]
            if scheduled:
                report["completion_probability_scheduled"] = (
                    outcomes["success"] / scheduled
                )
        except _ERRORS as error:
            errors.append(f"k6-summary.json counts: {error}")
        execution = _read(run_dir, "execution.json", errors)
        report["execution"] = execution
        if execution.get("k6_passed") is not True or execution.get("errors") != []:
            errors.append("k6 execution failed or its verdict is absent")
        report["diagnostics"] = _diagnostics(
            run_dir, protocol["function"], mode, errors
        )
        for node in ("stack", "cloud"):
            observed = sum(
                r["outcome"] == "success"
                and (
                    r["target"] == ("edge" if node == "stack" else "cloud")
                    or (
                        node == "cloud"
                        and r["target"] == "edge"
                        and r["observed_path"] == "offloaded"
                    )
                )
                for r in rows
            )
            diagnostic = report["diagnostics"][node]
            server = diagnostic["deltas"]["function_success_total"]
            diagnostic["observed_valid_successes"] = observed
            if server is not None and observed > server:
                errors.append(
                    f"{node}: valid client successes exceed server success delta"
                )
            diagnostic["server_minus_observed_success"] = (
                server - observed if server is not None else None
            )
            diagnostic["reconciliation_note"] = (
                "Server successes must cover valid client successes; transport loss "
                "may hide additional server completions. "
                "Edge successes already include offloads."
            )
        observed_offloads = sum(
            r["target"] == "edge" and r["observed_path"] == "offloaded" for r in rows
        )
        edge = report["diagnostics"]["stack"]
        edge["observed_offload_markers"] = observed_offloads
        if observed_offloads:
            counter = edge["counter_observations"]["nanofaas_offload_total"]
            delta = edge["deltas"]["nanofaas_offload_total"]
            if counter["after"] is None:
                errors.append("stack: observed offloads lack final offload telemetry")
            elif (delta if delta is not None else counter["after"]) < observed_offloads:
                errors.append(
                    "stack: observed offloads exceed corroborating offload counter"
                )
            elif counter["before"] is None:
                edge.setdefault("limitations", []).append(
                    "Offload baseline unavailable: final counter corroborates markers, "
                    "but no offload delta is claimed"
                )
    except _ERRORS as error:
        errors.append(f"experiment evidence: {error}")
    report["integrity_passed"] = not errors
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    fields: tuple[str, ...] = (
        "phase",
        "target",
        "iteration",
        "case_name",
        "status",
        "error_code",
        "outcome",
        "offloaded",
        "observed_path",
        "duration_ms",
        "http_duration_ms",
        "ts",
        "body",
    )
    with (run_dir / "measured-requests.csv").open("w", newline="") as stream:
        writer = csv.DictWriter[str](stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(r for r in rows if r["phase"] == "measurement")
    markdown = [
        "# B4 offload experiment",
        "",
        f"Evidence integrity: {'PASS' if not errors else 'FAIL'}",
        "",
        "```json",
        json.dumps(report.get("counts", {}), indent=2),
        "```",
        "",
        "Goodput (valid measurement-cohort completions/s scheduled window): "
        + str(report.get("goodput_per_second", "unavailable")),
        "",
        report["goodput_boundary"],
        report["scheduling_boundary"],
        "Completion probability uses actual scheduled requests, "
        "including boundary extras.",
        "Latency percentiles include valid successful measurement completions only.",
        "",
        "Offloaded is an observed response marker. Edge success already "
        "includes cloud success. No physical exactly-once or RAM claim.",
    ]
    if errors:
        markdown += ["", "Evidence failures:", "", *[f"- {e}" for e in errors]]
    (run_dir / "report.md").write_text("\n".join(markdown) + "\n")
    return report


def main() -> None:
    """Recompute an archived arm and fail the command on incomplete evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    result = analyze_experiment(parser.parse_args().run_dir)
    print(json.dumps({"integrity_passed": result["integrity_passed"]}))
    if not result["integrity_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
