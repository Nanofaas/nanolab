"""Recompute B6 memory comparisons from raw, labelled container observations."""

from __future__ import annotations

import argparse
import base64
import bisect
import json
import math
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from nanolab.assets.diagnostics.idle_reactivation_probe import equal, load_inputs
from nanolab.tasks.idle_reactivation import (
    parse_observation,
    qualifies_burst,
    qualifies_idle,
    qualifies_response,
)


def _epoch(value: str | float) -> float:
    return (
        datetime.fromisoformat(value).timestamp()
        if isinstance(value, str)
        else float(value)
    )


def _points(
    queries: dict[str, Any], name: str, shift: float
) -> dict[float, list[dict[str, Any]]]:
    indexed: dict[float, list[dict[str, Any]]] = {}
    for point in queries.get(name, {}).get("points", []):
        indexed.setdefault(_epoch(point["timestamp"]) - shift, []).append(point)
    return indexed


def _one(points: list[dict[str, Any]], pod: str) -> float | None:
    values = [
        float(p["value"]) for p in points if p.get("labels", {}).get("pod") == pod
    ]
    if len(values) != 1 or not math.isfinite(values[0]) or values[0] < 0:
        return None
    return values[0]


def _integral(
    points: list[tuple[float, float]], start: float, end: float
) -> float | None:
    # Use only this exposure: post-wake samples cannot describe idle memory.
    points = [(t, v) for t, v in points if start <= t <= end]
    if not points or points[0][0] - start > 5 or end - points[-1][0] > 5:
        return None
    points = [(start, points[0][1]), *points, (end, points[-1][1])]
    if any(b - a > 10 for (a, _), (b, _) in pairwise(points)):
        return None
    return sum((va + vb) * (b - a) / 2 for (a, va), (b, vb) in pairwise(points))


def memory_window(
    queries: dict[str, Any],
    observations: list[dict[str, Any]],
    start: float,
    end: float,
    function: str,
    *,
    shift: float = 0,
) -> dict[str, Any] | None:
    """Integrate non-overlapping containers; missing/ambiguous coverage stays absent."""
    if end <= start or not observations:
        return None
    observations = sorted(observations, key=lambda s: s["time"])
    times = [s["time"] for s in observations]
    health = _points(queries, "scrape_health", shift)
    result = {}
    for label, metric in (
        ("working_set", "container_memory_bytes"),
        ("rss", "container_rss_bytes"),
    ):
        control = _points(queries, metric + "@control-plane", shift)
        functions = _points(queries, metric + "@" + function, shift)
        values: dict[str, list[tuple[float, float]]] = {
            "control_plane": [],
            "function": [],
        }
        for timestamp in sorted(control):
            if not start <= timestamp <= end:
                continue
            index = bisect.bisect_right(times, timestamp) - 1
            if index < 0 or timestamp - times[index] > 5:
                continue
            state = observations[index]
            checks = [
                p
                for p in health.get(timestamp, [])
                if p.get("labels", {}).get("job") == "kubernetes-cadvisor"
            ]
            if (
                "error" in state
                or len(checks) != 1
                or float(checks[0]["value"]) != 1
                or len(state.get("control_plane", [])) != 1
            ):
                if start <= timestamp <= end:
                    return None
                continue
            cp = state["control_plane"][0]
            cp_value = _one(control[timestamp], cp["name"])
            pods = state.get("pods", [])
            if not pods and state.get("desired") == 0 and state.get("ready") == 0:
                fn_value = 0.0
            elif len(pods) == 1:
                fn_value = _one(functions.get(timestamp, []), pods[0]["name"])
            else:
                fn_value = None
            if cp_value is None or fn_value is None:
                if start <= timestamp <= end:
                    return None
                continue
            values["control_plane"].append((timestamp, cp_value / 1024**2))
            values["function"].append((timestamp, fn_value / 1024**2))
        integrals = {
            name: _integral(points, start, end) for name, points in values.items()
        }
        cp_integral, fn_integral = integrals["control_plane"], integrals["function"]
        if cp_integral is None or fn_integral is None:
            return None
        total = cp_integral + fn_integral
        result[label] = {
            "mean_mib": total / (end - start),
            "mib_seconds": total,
            "control_plane_mean_mib": cp_integral / (end - start),
            "function_mean_mib": fn_integral / (end - start),
        }
    return result


def verify_ledger(
    path: Path,
    fixtures: list[dict[str, Any]],
    function: str,
    phase: str,
    cycle: int,
    seen: set[str],
) -> bool:
    """Verify complete attempts and exact bodies against the archived oracle."""
    try:
        events = [json.loads(line) for line in path.read_text().splitlines()]
        expected = {"qualification": 4, "warmup": 60, "cycle": 6}[phase]
        if len(events) != 2 * expected:
            return False
        for part in range(expected):
            started, response = events[2 * part : 2 * part + 2]
            fixture = fixtures[(cycle if phase == "cycle" else part) % 4]
            identity = {
                "phase": phase,
                "cycle": cycle,
                "part": part,
                "function": function,
                "case_name": fixture["name"],
            }
            request_id = started["request_id"]
            if (
                not request_id
                or request_id in seen
                or response["request_id"] != request_id
                or started["event"] != "started"
                or response["event"] != "response"
                or any(started[k] != v or response[k] != v for k, v in identity.items())
            ):
                return False
            seen.add(request_id)
            body = json.loads(base64.b64decode(response["body_base64"], validate=True))
            duration = (
                response["ended_monotonic_ns"] - response["started_monotonic_ns"]
            ) / 1_000_000
            if (
                response["status"] != 200
                or response["error"] is not None
                or not response["valid"]
                or body.get("status") != "success"
                or not equal(body.get("output"), fixture["expected"])
                or not math.isfinite(duration)
                or duration < 0
                or not math.isclose(duration, response["duration_ms"])
            ):
                return False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return True


def verify_lifecycle(
    summary: dict[str, Any], observations: list[dict[str, Any]]
) -> bool:
    """Reapply lifecycle gates to raw Kubernetes evidence, not stored verdicts."""
    try:
        measured = [
            s for s in observations if s["time"] >= summary["measurement_start"]
        ]
        if not measured or len(summary["cycles"]) != summary["configured_cycles"]:
            return False
        first = measured[0]
        cp, fn = first["control_plane"][0], first["pods"][0]
        summary["control_plane_uid"] = cp["uid"]
        summary["identities"] = {
            "control_plane": cp["image_id"],
            "function": fn["image_id"],
        }
        if any(
            "error" in s
            or len(s.get("control_plane", [])) != 1
            or s["control_plane"][0]["uid"] != cp["uid"]
            or s["control_plane"][0]["image_id"] != cp["image_id"]
            or s["control_plane"][0]["restarts"] != 0
            or not s["control_plane"][0]["ready"]
            for s in measured
        ):
            return False
        for index, cycle in enumerate(summary["cycles"]):
            if cycle["cycle"] != index:
                return False
            before = next(
                s for s in reversed(observations) if s["time"] <= cycle["idle_end"]
            )
            after = next(s for s in reversed(observations) if s["time"] <= cycle["end"])
            seen = {
                p["uid"]
                for s in observations
                if s["time"] <= before["time"]
                for p in s.get("pods", [])
            }
            if (
                not qualifies_idle(
                    observations,
                    cycle["idle_end"] - 60,
                    cycle["idle_end"],
                    summary["treatment"],
                )
                or not qualifies_response(
                    before, after, summary["treatment"], seen, fn["image_id"]
                )
                or not qualifies_burst(
                    observations, before["time"], after["time"], after
                )
            ):
                return False
        tail = summary["tail"]
        return qualifies_idle(
            observations, tail["end"] - 60, tail["end"], summary["treatment"]
        )
    except (KeyError, TypeError, IndexError, StopIteration):
        return False


def _analyze_arm(run_dir: Path, name: str, seen: set[str]) -> dict[str, Any]:
    fixtures = load_inputs(
        run_dir / "inputs/performance-medium.json",
        run_dir / "inputs/runtime-contract.json",
    )
    root = run_dir / name
    summary = json.loads((root / "summary.json").read_text())
    snapshot_path = root / "metrics/prometheus-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else None
    observations = []
    for line in (root / "lifecycle.jsonl").read_text().splitlines():
        record = json.loads(line)
        try:
            observations.append(
                parse_observation(record["raw"], summary["function"], record["time"])
            )
        except (ValueError, KeyError, TypeError):
            observations.append(
                {"time": record["time"], "error": "Missing or invalid raw observation"}
            )
    summary["lifecycle_reverified"] = verify_lifecycle(summary, observations)
    memory = []
    if snapshot:
        # CapturePrometheusSnapshot shifts the whole host-clock interval as one.
        shift = _epoch(snapshot["start"]) - _epoch(summary["start"])
        memory = [
            {
                "cycle": cycle["cycle"],
                "idle": memory_window(
                    snapshot["queries"],
                    observations,
                    cycle["idle_end"] - 60,
                    cycle["idle_end"],
                    summary["function"],
                    shift=shift,
                ),
                "whole_cycle": memory_window(
                    snapshot["queries"],
                    observations,
                    cycle["idle_start"],
                    cycle.get("end", cycle["idle_end"]),
                    summary["function"],
                    shift=shift,
                ),
            }
            for cycle in summary["cycles"]
        ]
    phases = [("qualification", 0), ("warmup", 0)] + [
        ("cycle", i) for i in range(summary["configured_cycles"])
    ]
    summary["requests_qualified"] = all(
        verify_ledger(
            root / f"{phase}-{cycle:04d}.jsonl",
            fixtures,
            summary["function"],
            phase,
            cycle,
            seen,
        )
        for phase, cycle in phases
    )
    summary["idle_memory"] = memory
    summary["memory_qualified"] = len(memory) == summary["configured_cycles"] and all(
        m["idle"] is not None for m in memory
    )
    return summary


def analyze_pair(run_dir: Path) -> dict[str, Any]:
    """Rebuild the paired report, retaining interrupted arms as incomplete."""
    arms = {}
    seen: set[str] = set()
    for name in ("keep-ready", "scale-to-zero"):
        try:
            arms[name] = _analyze_arm(run_dir, name, seen)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            arms[name] = {
                "complete": False,
                "errors": [str(error)],
                "outcome": "incomplete",
            }
    ready, zero = arms.values()
    passed = (
        all(
            a["complete"]
            and a["memory_qualified"]
            and a["requests_qualified"]
            and a["lifecycle_reverified"]
            for a in arms.values()
        )
        and bool(ready.get("identities"))
        and ready["identities"] == zero.get("identities")
        and ready["function"] == zero["function"]
        and ready["configured_cycles"] == zero["configured_cycles"]
        and bool(ready.get("control_plane_uid"))
        and bool(zero.get("control_plane_uid"))
        and ready["control_plane_uid"] != zero["control_plane_uid"]
    )
    result = {
        "passed": passed,
        "arms": arms,
        "memory_boundary": "control-plane and function containers",
        "memory_method": (
            "within-window samples only; boundary hold at most 5s; "
            "trapezoidal integral; "
            "maximum sample gap 10s"
        ),
        "latency_boundary": (
            "client send to full response body; fresh connection per request"
        ),
    }
    (run_dir / "paired-summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    """Reproduce the B6 evidence gate from an archived run directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    result = analyze_pair(args.run_dir)
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "report": str(args.run_dir / "paired-summary.json"),
            }
        )
    )
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
