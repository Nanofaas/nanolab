"""Offline verification of matched module costs from original observations."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from nanolab.assets.diagnostics.idle_reactivation_probe import equal, load_inputs
from nanolab.comparison.cells import write_json_atomically
from nanolab.comparison.experiment_01 import read_events
from nanolab.comparison.module_cost import module_cost_cells
from nanolab.config.module_cost import ARMS, BUDGETS
from nanolab.release.recipe_execution import verify_source_inventory
from nanolab.tasks.idle_reactivation import parse_observation, qualifies_idle
from nanolab.tasks.idle_reactivation_report import memory_window
from nanolab.tasks.module_cost import FUNCTION, fixed_function
from nanolab.tasks.recipes.workflow import read_distribution


def require(condition: bool, message: str) -> None:
    """Reject invalid evidence explicitly."""
    if not condition:
        raise ValueError(message)


def epoch(value):
    """Convert a saved timestamp into seconds."""
    return (
        datetime.fromisoformat(value).timestamp()
        if isinstance(value, str)
        else float(value)
    )


def metric(summary, name):
    """Read either supported k6 summary encoding."""
    item = summary.get("metrics", {}).get(name, {})
    return item.get("values", item).get("count", 0)


def read_load(
    directory: Path, inputs: Path, seconds: int, phase: str
) -> dict[str, Any]:
    """Reconcile every attempt and independently verify exact response bodies."""
    fixtures = load_inputs(
        inputs / "performance-medium.json", inputs / "runtime-contract.json"
    )
    events, malformed = read_events(directory / "events.jsonl")
    require(not malformed, "malformed request ledger")
    sent, terminal = {}, {}
    for event in events:
        kind, key = event.get("t"), event.get("id")
        require(
            kind in ("send", "terminal") and isinstance(key, str),
            "unknown ledger event",
        )
        target = sent if kind == "send" else terminal
        require(key not in target, "duplicate request event")
        target[key] = event
    require(
        bool(sent) and sent.keys() == terminal.keys(), "missing or unresolved request"
    )
    counts, statuses, latencies = Counter(), Counter(), []
    valid_count = 0
    for key, result in terminal.items():
        start = sent[key]
        kind, index = start["phase"], start["iteration"]
        require(
            kind in ("qualification", phase) and type(index) is int and index >= 0,
            "foreign request",
        )
        require(key == f"{phase}:{kind}:{index}", "foreign request identity")
        fixture = fixtures[index % len(fixtures)]
        require(
            all(result[k] == start[k] for k in ("phase", "iteration", "case_name")),
            "changed request identity",
        )
        require(start["case_name"] == fixture["name"], "wrong corpus case")
        duration = result["ts"] - start["ts"]
        require(
            math.isfinite(duration)
            and duration >= 0
            and result["started_at"] == start["ts"]
            and result["duration_ms"] == duration,
            "invalid request timing",
        )
        try:
            body = json.loads(result["body"])
            correct = (
                result["status"] == 200
                and body.get("status") == "success"
                and equal(body.get("output"), fixture["expected"])
            )
        except (ValueError, AttributeError):
            correct = False
        require(result["valid"] is correct, "oracle result differs from response body")
        require(result["status"] != 200 or correct, "HTTP 200 with invalid output")
        counts[kind] += 1
        if kind == "qualification":
            require(correct and index < 4, "qualification failed")
        else:
            statuses[str(result["status"])] += 1
            valid_count += int(correct)
            if correct:
                latencies.append(duration)
    summary = json.loads((directory / "summary.json").read_text())
    iterations, drops = (
        metric(summary, "iterations"),
        metric(summary, "dropped_iterations"),
    )
    require(counts["qualification"] == 4, "missing qualification")
    require(iterations == counts[phase], "k6 iterations differ from ledger")
    require(
        iterations + drops in (40 * seconds, 40 * seconds + 1),
        "incomplete arrival calendar",
    )
    require(
        metric(summary, "http_reqs") == len(sent), "HTTP requests differ from ledger"
    )
    # Integer-time boundary arrival can add one request; retain the actual cohort.
    cohort_sends = [e["ts"] for e in sent.values() if e["phase"] == phase]
    cohort_ends = [e["ts"] for e in terminal.values() if e["phase"] == phase]
    require(bool(cohort_sends), "no emitted load cohort")
    ordered = sorted(latencies)

    def percentile(p):
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)] if ordered else None

    return {
        "first_send_ms": min(cohort_sends),
        "last_terminal_ms": max(cohort_ends),
        "scheduled": iterations + drops,
        "emitted": iterations,
        "dropped": drops,
        "valid": valid_count,
        "status_counts": dict(statuses),
        "goodput_rps": valid_count / seconds,
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "latency_population": "valid whole responses, including drain",
    }


def cohort_window(load: dict, anchors: list[dict]) -> tuple[float, float]:
    """Map request timestamps to controller time with bounded clock uncertainty."""
    require(len(anchors) == 2, "missing generator clock anchors")
    offsets = []
    for anchor in anchors:
        sent, received, remote = (
            float(anchor[k]) for k in ("sent", "received", "remote")
        )
        require(
            all(math.isfinite(v) for v in (sent, received, remote))
            and 0 <= received - sent <= 2,
            "generator clock uncertainty",
        )
        offsets.append(remote - (sent + received) / 2)
    require(abs(offsets[1] - offsets[0]) <= 1, "generator clock drift")
    offset = sum(offsets) / 2
    start, end = (
        load["first_send_ms"] / 1000 - offset,
        load["last_terminal_ms"] / 1000 - offset,
    )
    require(
        anchors[0]["sent"] - 1 <= start < end <= anchors[1]["received"] + 1,
        "request cohort outside clock anchors",
    )
    return start, end


def concurrency_coverage(points, timestamps, start, end, shift):
    """Require one stable sample per timestamp and bounded metric coverage."""
    values = sorted(
        (epoch(p["timestamp"]) - shift, float(p["value"]))
        for p in points
        if start <= epoch(p["timestamp"]) - shift <= end
    )
    require(
        len(values) >= 2
        and values[0][0] - start <= 5
        and end - values[-1][0] <= 5
        and all(0 < b[0] - a[0] <= 10 for a, b in pairwise(values)),
        "effective concurrency coverage missing or ambiguous",
    )
    require(all(v == 2 for _, v in values), "effective concurrency differs")
    scrapes = sorted(
        (epoch(p["timestamp"]) - shift, float(p["value"]) - shift)
        for p in timestamps
        if start <= epoch(p["timestamp"]) - shift <= end
    )
    require(
        [t for t, _ in scrapes] == [t for t, _ in values]
        and all(
            math.isfinite(scraped) and 0 <= t - scraped <= 10 for t, scraped in scrapes
        ),
        "effective concurrency stale or missing scrape timestamps",
    )


def cpu_window(points, pod: str, start: float, end: float, shift: float = 0) -> float:
    """Raw counter slope over observed coverage; reject resets and ambiguity."""
    values = sorted(
        (epoch(p["timestamp"]) - shift, float(p["value"]))
        for p in points
        if p.get("labels", {}).get("pod") == pod
        and start <= epoch(p["timestamp"]) - shift <= end
    )
    require(
        len(values) >= 2 and values[0][0] - start <= 5 and end - values[-1][0] <= 5,
        "CPU coverage missing",
    )
    require(all(math.isfinite(v) and v >= 0 for _, v in values), "invalid CPU counter")
    for (a, va), (b, vb) in pairwise(values):
        require(0 < b - a <= 10, "CPU coverage ambiguous or incomplete")
        require(vb >= va, "CPU counter reset")
    return (values[-1][1] - values[0][1]) / (values[-1][0] - values[0][0])


def contrasts(values: dict[str, float]) -> dict[str, float]:
    """Compute signed within-block effects."""
    return {
        "c-base": values["c"] - values["base"],
        "s-base": values["s"] - values["base"],
        "cs-base": values["cs"] - values["base"],
        "interaction": values["cs"] - values["c"] - values["s"] + values["base"],
    }


def subset(expected, actual) -> bool:
    """Check explicit policy fields while allowing resolved defaults."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and subset(v, actual[k]) for k, v in expected.items()
        )
    return expected == actual


def analyze_arm(block: Path, arm: str, protocol: dict) -> dict[str, Any]:
    """Validate evidence before deriving one arm's costs."""
    directory = block / "arms" / arm

    def load(path):
        return json.loads(path.read_text())

    report = load(directory / "measurement.json")
    require(
        report["status"] == "collected" and not report.get("collection_errors"),
        "incomplete collection",
    )
    require(not (directory / "failure.txt").exists(), "failed deployment")
    require(
        (directory / "boot-id.txt").read_text().strip() == protocol["host"]["boot_id"],
        "replacement host",
    )
    images = load(directory / "deployment.json")["images"]
    function_image = load(block / "build/jvm-base/images.json")["function"]["digest"]
    require(images["function"] == function_image, "different function image")
    expected = fixed_function(function_image).manifest().body()
    require(load(directory / "registration.json") == expected, "registration drift")
    for when in ("before", "after"):
        require(
            subset(expected, load(directory / f"spec-{when}.json")),
            "effective function policy drift",
        )
    recipe = block / "build" / arm / "recipe.yaml"
    require(
        yaml.safe_load(recipe.read_text())
        == module_cost_cells(protocol["protocol"]["blockId"])[arm],
        "recipe protocol drift",
    )
    distribution = read_distribution(
        block / "build" / arm / "distribution.json",
        recipe=block / "build" / arm / "recipe.yaml",
        tag=protocol["protocol"]["blockId"],
        published=False,
    )
    logs = (directory / "control-plane.log").read_text()
    for module, marker in (
        ("concurrency-control", "ConcurrencyGovernor starting with poll interval"),
        ("autoscaler", "InternalScaler starting with poll interval"),
    ):
        require(
            (marker in logs) == (module in distribution.modules),
            f"module initialization differs: {module}",
        )
    raw = [
        json.loads(line)
        for line in (directory / "lifecycle.jsonl").read_text().splitlines()
    ]
    observations = [parse_observation(s["raw"], FUNCTION, s["time"]) for s in raw]
    start, end = report["idle_end"] - 120, report["idle_end"]
    require(report["idle_end"] - report["idle_start"] >= 180, "short idle interval")
    require(
        qualifies_idle(observations, report["idle_start"], report["end"], "keep-ready"),
        "pod replacement or lifecycle gap",
    )
    first = observations[0]
    for component, key in (("control-plane", "control_plane"), ("function", "pods")):
        actual = first[key][0]["image_id"].split("@")[-1].removeprefix("containerd://")
        require(
            actual == images[component].split("@")[-1], f"{component} image differs"
        )
    for sample in raw:
        for pod in (p for p in sample["raw"]["items"] if p["kind"] == "Pod"):
            for container in pod["spec"]["containers"]:
                component = container["name"]
                if component not in ("control-plane", "function"):
                    continue
                budget = BUDGETS[
                    "control-plane" if component == "control-plane" else FUNCTION
                ]
                resources = container["resources"]
                for kind in ("requests", "limits"):
                    cpu = str(resources[kind]["cpu"])
                    value = float(cpu[:-1]) / 1000 if cpu.endswith("m") else float(cpu)
                    memory = resources[kind]["memory"]
                    memory_mib = float(memory[:-2]) * (
                        1024 if memory.endswith("Gi") else 1
                    )
                    require(
                        value == budget[kind]["cpu"]
                        and memory_mib == budget[kind]["memoryMiB"],
                        "container budget drift",
                    )
    snapshot = load(directory / "metrics/prometheus-snapshot.json")
    shift = epoch(snapshot["start"]) - report["start"]
    queries = snapshot["queries"]
    warm = read_load(directory / "warmup", block / "inputs", 60, "warmup")
    require(
        warm["valid"] == warm["emitted"] and not warm["dropped"],
        "warmup did not drain successfully",
    )
    measured = read_load(
        directory / "measurement", block / "inputs", 180, "measurement"
    )
    warm_window = cohort_window(warm, load(directory / "warmup/clock.json"))
    load_window = cohort_window(measured, load(directory / "measurement/clock.json"))
    require(warm_window[1] <= report["idle_start"], "warmup overlaps idle")
    require(tuple(report["load_window"]) == load_window, "load boundary drift")
    concurrency_coverage(
        queries["function_effective_concurrency"]["points"],
        queries["function_effective_concurrency_timestamp"]["points"],
        warm_window[0],
        load_window[1],
        shift,
    )
    memory = memory_window(queries, observations, start, end, FUNCTION, shift=shift)
    require(memory is not None, "memory coverage missing")
    cpu = {
        component: cpu_window(
            queries[f"container_cpu_seconds@{name}"]["points"],
            first[key][0]["name"],
            start,
            end,
            shift,
        )
        for component, name, key in (
            ("control_plane", "control-plane", "control_plane"),
            ("function", FUNCTION, "pods"),
        )
    }
    # Kubernetes timestamps share the stack clock; exclude scheduler and cached pulls.
    cp = next(
        p
        for p in raw[0]["raw"]["items"]
        if p["kind"] == "Pod"
        and p["metadata"]["uid"] == first["control_plane"][0]["uid"]
    )
    status = next(
        c for c in cp["status"]["containerStatuses"] if c["name"] == "control-plane"
    )
    ready = next(
        c
        for c in cp["status"]["conditions"]
        if c["type"] == "Ready" and c["status"] == "True"
    )
    readiness = epoch(ready["lastTransitionTime"]) - epoch(
        status["state"]["running"]["startedAt"]
    )
    require(readiness >= 0, "invalid readiness timestamps")
    load_memory = memory_window(
        queries,
        observations,
        load_window[0],
        load_window[1],
        FUNCTION,
        shift=shift,
    )
    require(load_memory is not None, "load memory coverage missing")
    load_cpu = {
        component: cpu_window(
            queries[f"container_cpu_seconds@{name}"]["points"],
            first[key][0]["name"],
            load_window[0],
            load_window[1],
            shift,
        )
        for component, name, key in (
            ("control_plane", "control-plane", "control_plane"),
            ("function", FUNCTION, "pods"),
        )
    }
    sizes = load(block / "build" / arm / "artifact-files.json")
    return {
        "idle": memory,
        "load_resources": {"memory": load_memory, "cpu_cores": load_cpu},
        "idle_cpu_cores": cpu,
        "load": measured,
        "readiness_seconds": readiness,
        "readiness_boundary": (
            "container running to Pod Ready; Kubernetes clock, 1s resolution"
        ),
        "artifact_files_bytes": sizes,
        "image_bytes": load(block / "build" / arm / "images.json")["control-plane"][
            "uncompressed_image_bytes"
        ],
        "control_plane_uid": first["control_plane"][0]["uid"],
        "function_image": function_image,
    }


def analyze_module_cost(block: Path) -> dict[str, Any]:
    """Keep non-comparable arms and their reasons, never manufacture contrasts."""
    protocol = json.loads((block / "protocol.json").read_text())
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "protocol": protocol,
        "arms": {},
        "errors": {},
        "contrasts": {},
    }
    for arm in ARMS:
        try:
            inventory = json.loads((block / "inputs/source-inventory.json").read_text())
            current = json.loads(
                (block / "build" / arm / "source-inventory.json").read_text()
            )
            verify_source_inventory(inventory, current, Path("/source"))
            result["arms"][arm] = analyze_arm(block, arm, protocol)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            StopIteration,
        ) as error:
            result["errors"][arm] = str(error) or type(error).__name__
    if not result["errors"]:
        uids = [a["control_plane_uid"] for a in result["arms"].values()]
        require(len(set(uids)) == 8, "deployments reused across arms")
        for runtime in ("jvm", "native"):
            result["contrasts"][runtime] = {}
            for metric_name, getter in (
                (
                    "working_set_mib",
                    lambda a: a["idle"]["working_set"]["control_plane_mean_mib"],
                ),
                ("rss_mib", lambda a: a["idle"]["rss"]["control_plane_mean_mib"]),
                ("cpu_cores", lambda a: a["idle_cpu_cores"]["control_plane"]),
                ("image_bytes", lambda a: a["image_bytes"]),
                ("readiness_seconds", lambda a: a["readiness_seconds"]),
            ):
                result["contrasts"][runtime][metric_name] = contrasts(
                    {
                        name: getter(result["arms"][f"{runtime}-{name}"])
                        for name in ("base", "c", "s", "cs")
                    }
                )
    result["comparable"] = not result["errors"]
    write_json_atomically(block / "analysis.json", result)
    return result


def main():
    """Regenerate a block analysis and fail if it is not comparable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("block", type=Path)
    args = parser.parse_args()
    result = analyze_module_cost(args.block)
    if not result["comparable"]:
        raise SystemExit(json.dumps(result["errors"], indent=2))


if __name__ == "__main__":
    main()
