"""VM telemetry policy, phase correlation and raw coverage accounting."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from nanolab.config.one_shot import ArtifactReference, OneShotConfig
from nanolab.tasks.one_shot.artifacts import (
    canonical_bytes,
    package_fingerprint,
)

# The workbench Prometheus scrape period; vm-resources rows are one per scrape.
SCRAPE_SECONDS = 5


def telemetry_policy() -> dict:
    """Return the policy a run freezes; `policy.json` adds the exporter images.

    The images are pinned by the workbench collection version in the bundled
    requirements.yml, which `package_fingerprint` already binds.
    """
    return {
        "schemaVersion": 1,
        "periodSeconds": SCRAPE_SECONDS,
        "scope": "whole-vm-and-all-running-docker-cgroups-v2",
        "cpuUnit": "fraction-of-vcpu-capacity",
        "memoryUnit": "bytes",
        "warmupIncluded": True,
        "transport": "prometheus-node-exporter-cadvisor",
        "phaseCorrelation": "prometheus-scrape-utc",
    }


# What the removed VM sampler froze: the policy of every run before the
# workbench, and the default for an archived run without a policy.json.
LEGACY_SAMPLER_POLICY = {
    "schemaVersion": 1,
    "periodSeconds": 1,
    "scope": "whole-vm-and-all-running-docker-cgroups-v2",
    "cpuUnit": "fraction-of-vcpu-capacity",
    "memoryUnit": "bytes",
    "warmupIncluded": True,
    "transport": "multipass-pipes-with-owned-drain-threads",
    "samplerSha256": (
        "5517e0e1ff41853627da76f539b5c98131a16baf3d1795b1e2379fe2ab539b87"
    ),
    "phaseCorrelation": "v2-host-guest-monotonic-brackets-with-explicit-ambiguity",
}


def correlate_sample(
    sample: dict,
    *,
    anchor: datetime,
    warmup: int,
    period: int,
    epochs: int,
    mode: str,
    functions: list[str],
    run_id: str,
    host_anchor_monotonic: float | None = None,
    clock_probes: list[dict] | None = None,
) -> dict:
    """Use bracketing monotonic clocks for v2, preserving ambiguous boundaries."""
    timestamp = datetime.fromisoformat(sample["timestampUtc"])
    if timestamp.tzinfo is None or anchor.tzinfo is None:
        raise ValueError("resource timestamps must have UTC timezone")
    row = {**sample, "runId": run_id, "mode": mode, "functions": functions}
    if host_anchor_monotonic is not None:
        monotonic = sample.get("monotonicSeconds")
        boot = sample.get("bootId")
        probes = [
            probe
            for probe in clock_probes or []
            if probe.get("node") == sample.get("node")
            and probe.get("remoteBootId") == boot
            and boot
            and probe.get("errorType") is None
            and all(
                isinstance(probe.get(key), (int, float))
                and not isinstance(probe[key], bool)
                and math.isfinite(probe[key])
                for key in (
                    "remoteMonotonicSeconds",
                    "beforeMonotonicSeconds",
                    "afterMonotonicSeconds",
                )
            )
            and probe["beforeMonotonicSeconds"] <= probe["afterMonotonicSeconds"]
        ]
        if not isinstance(monotonic, (int, float)) or not math.isfinite(monotonic):
            probes = []
        previous = [p for p in probes if p["remoteMonotonicSeconds"] <= monotonic]
        following = [p for p in probes if p["remoteMonotonicSeconds"] >= monotonic]
        if not previous or not following:
            return {
                **row,
                "phase": "uncorrelated",
                "epoch": None,
                "timeFromAnchorSeconds": None,
                "epochCandidates": [],
                "correlationMethod": "host-guest-monotonic-brackets",
                "correlationError": "missing bracketing probes or boot identity",
            }
        before = max(previous, key=lambda p: p["remoteMonotonicSeconds"])
        after = min(following, key=lambda p: p["remoteMonotonicSeconds"])
        lower = before["beforeMonotonicSeconds"] - host_anchor_monotonic
        upper = after["afterMonotonicSeconds"] - host_anchor_monotonic
        if lower > upper:
            raise ValueError("inconsistent host/guest monotonic ordering")
        before_mid = (
            before["beforeMonotonicSeconds"] + before["afterMonotonicSeconds"]
        ) / 2
        after_mid = (
            after["beforeMonotonicSeconds"] + after["afterMonotonicSeconds"]
        ) / 2
        span = after["remoteMonotonicSeconds"] - before["remoteMonotonicSeconds"]
        fraction = (monotonic - before["remoteMonotonicSeconds"]) / span if span else 0
        offset = (
            before_mid + fraction * (after_mid - before_mid) - host_anchor_monotonic
        )
        candidates = {
            resource_phase(lower, warmup, period, epochs),
            resource_phase(upper, warmup, period, epochs),
        }
        for boundary in [
            -warmup,
            *(range(0, period * epochs + 1, period) if period else [0]),
        ]:
            if lower <= boundary <= upper:
                candidates.add(resource_phase(boundary, warmup, period, epochs))
        phase, epoch = resource_phase(offset, warmup, period, epochs)
        if len(candidates) > 1:
            phase, epoch = "ambiguous", None
        return {
            **row,
            "phase": phase,
            "epoch": epoch,
            "timeFromAnchorSeconds": offset,
            "correlationBoundsSeconds": [lower, upper],
            "epochCandidates": sorted({e for _, e in candidates if e is not None}),
            "phaseCandidates": sorted({p for p, _ in candidates}),
            "correlationMethod": "host-guest-monotonic-brackets",
        }
    offset = (timestamp - anchor).total_seconds()
    phase, epoch = resource_phase(offset, warmup, period, epochs)
    return {
        **row,
        "phase": phase,
        "epoch": epoch,
        "timeFromAnchorSeconds": offset,
        "correlationMethod": "legacy-utc",
    }


def resource_phase(
    offset: float, warmup: int, period: int, epochs: int
) -> tuple[str, int | None]:
    """Classify one point on the frozen experiment timeline."""
    phase, epoch = ("before", None)
    if -warmup <= offset < 0:
        phase, epoch = "warmup", -1
    elif 0 <= offset < period * epochs:
        phase, epoch = "measurement", int(offset // period)
    elif offset >= period * epochs:
        phase = "after"
    return phase, epoch


def resource_clock_grid(run_dir: Path) -> dict | None:
    """Resolve the manifest's versioned clock scope for all probe consumers."""
    manifest = run_dir / "manifest.json"
    if not manifest.exists():
        return None
    grid = json.loads(manifest.read_text()).get("parameters", {}).get("logicalSchedule")
    if grid is None:
        return None
    if grid.get("protocolVersion") != 2:
        raise ValueError("unsupported resource correlation protocol")
    scope = grid.get("probeScope", "run")
    if scope not in {"run", "parent"}:
        raise ValueError("unsupported resource clock probe scope")
    return grid


def resource_probe_paths(run_dir: Path) -> tuple[Path, Path]:
    """Use the same frozen probe scope for correlation and malformed records."""
    grid = resource_clock_grid(run_dir)
    probe_dir = run_dir
    if grid and grid.get("probeScope") == "parent":
        levels = grid.get("probeParentLevels", 1)
        if type(levels) is not int or not 1 <= levels <= len(run_dir.parents):
            raise ValueError("invalid resource clock probe parent levels")
        probe_dir = run_dir.parents[levels - 1]
    return probe_dir / "preflight-probes.jsonl", probe_dir / "clock-probes.jsonl"


def resource_policy(run_dir: Path) -> dict:
    """Return the run's frozen telemetry policy; runs without one were sampled."""
    path = run_dir / "vm-resources" / "policy.json"
    return json.loads(path.read_text()) if path.exists() else LEGACY_SAMPLER_POLICY


def resource_clock_context(run_dir: Path) -> dict:
    """Read original successful and failed probes without repairing raw files."""
    grid = resource_clock_grid(run_dir)
    # Prometheus rows carry scrape UTC and no guest monotonic clock: brackets
    # cannot place them, so they correlate on UTC like the legacy path.
    if grid is None or resource_policy(run_dir).get("phaseCorrelation") == (
        "prometheus-scrape-utc"
    ):
        return {}
    return {
        "host_anchor_monotonic": grid["anchorMonotonicSeconds"],
        "clock_probes": [
            row
            for path in resource_probe_paths(run_dir)
            for row in read_samples(path)[0]
        ],
    }


def read_samples(path: Path) -> tuple[list[dict], int]:
    """Never truncate or repair raw evidence; retain a partial terminal line."""
    rows, malformed = [], 0
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("sample is not an object")
                rows.append(row)
            except (ValueError, TypeError):
                malformed += 1
    return rows, malformed


def summarize_resources(
    run_dir: Path,
    *,
    anchor: datetime,
    warmup: int,
    period: int,
    epochs: int,
    nodes: list[str],
    mode: str,
    functions: list[str],
    run_id: str,
) -> dict:
    """Count every expected slot of the policy's period, including absent nodes."""
    policy = resource_policy(run_dir)
    result: dict[str, Any] = {
        "policy": policy,
        "complete": True,
        "nodes": {},
    }
    malformed_probes = sum(
        read_samples(path)[1] for path in resource_probe_paths(run_dir)
    )
    result["clockProbeMalformedLines"] = malformed_probes
    if malformed_probes:
        result["complete"] = False
    step = policy.get("periodSeconds", 1)
    # One slot per policy period, labelled by its first second; at 1 s this is
    # exactly the per-second count every older run was summarized with.
    expected = {second - second % step for second in range(-warmup, period * epochs)}
    clock_context = resource_clock_context(run_dir)
    for node in nodes:
        path = run_dir / "vm-resources" / (node + ".jsonl")
        rows, malformed = read_samples(path)
        counts: dict[int, int] = {}
        errors = []
        container_errors = []
        missing_containers = []
        invalid = []
        roles = set()
        uncertain, uncorrelated = 0, 0
        for index, raw in enumerate(rows):
            try:
                row = correlate_sample(
                    raw,
                    anchor=anchor,
                    warmup=warmup,
                    period=period,
                    epochs=epochs,
                    mode=mode,
                    functions=functions,
                    run_id=run_id,
                    **clock_context,
                )
                if row["phase"] == "uncorrelated":
                    uncorrelated += 1
                    continue
                if row["phase"] == "ambiguous":
                    uncertain += 1
                slot = int(row["timeFromAnchorSeconds"] // step) * step
                if slot not in expected:
                    continue
                if row.get("node") != node:
                    raise ValueError("raw node identity mismatch")
                counts[slot] = counts.get(slot, 0) + 1
                required = [
                    "memoryTotalBytes",
                    "memoryAvailableBytes",
                    "memoryUsedBytes",
                    "swapTotalBytes",
                    "swapFreeBytes",
                    "swapUsedBytes",
                    "vcpuCount",
                ]
                if any(
                    not isinstance(row.get(key), (int, float)) or row[key] < 0
                    for key in required
                ):
                    errors.append(
                        {
                            "second": slot,
                            "error": "whole-VM memory/swap/vCPU counters unavailable",
                        }
                    )
                if row.get("cpuUtilization") is None:
                    invalid.append(slot)
                for error in row.get("errors", []):
                    (container_errors if error.get("containerId") else errors).append(
                        {"second": slot, **error}
                    )
                missing_ids = set(row.get("expectedContainerIds", [])) - {
                    container["containerId"] for container in row.get("containers", [])
                }
                if missing_ids:
                    missing_containers.append(
                        {"second": slot, "containerIds": sorted(missing_ids)}
                    )
                roles.update(
                    container["role"] for container in row.get("containers", [])
                )
            except (ValueError, KeyError, TypeError) as error:
                errors.append({"line": index + 1, "error": str(error)})
        missing = sorted(expected - counts.keys())
        duplicates = sorted(slot for slot, count in counts.items() if count > 1)
        complete = not (
            missing
            or duplicates
            or malformed
            or errors
            or invalid
            or container_errors
            or missing_containers
            or uncorrelated
        )
        result["nodes"][node] = {
            "complete": complete,
            "expectedSamples": len(expected),
            "observedSeconds": len(counts),
            "missingSeconds": missing,
            "duplicateSeconds": duplicates,
            "unavailableCpuSeconds": invalid,
            "uncertainPhaseSamples": uncertain,
            "uncorrelatedSamples": uncorrelated,
            "phaseCorrelationComplete": not (uncertain or uncorrelated),
            "malformedLines": malformed,
            "errors": errors,
            "containerErrors": container_errors,
            "missingContainerSamples": missing_containers,
            "observedRoles": sorted(roles),
            "rawPath": str(path.relative_to(run_dir)),
            "rawSha256": hashlib.sha256(path.read_bytes()).hexdigest()
            if path.exists()
            else None,
        }
        result["complete"] &= complete
    return result


def qualify_overhead(pairs: list[dict], *, expected_cells: set[str]) -> dict:
    """Require three independent paired tests and <=5% cost in every cell."""
    if len(pairs) != 3 or not expected_cells:
        raise ValueError("telemetry overhead needs three complete paired trials")
    qualified = True
    results = []
    for index, pair in enumerate(pairs):
        if set(pair["off"]) != expected_cells or set(pair["on"]) != expected_cells:
            raise ValueError("telemetry overhead node/function scope incomplete")
        cells = {}
        for key in sorted(expected_cells):
            off, on = pair["off"][key], pair["on"][key]
            values = [
                row[field] for row in [off, on] for field in ["goodput", "p95Seconds"]
            ]
            if not all(
                isinstance(v, (int, float)) and math.isfinite(v) and v > 0
                for v in values
            ):
                raise ValueError("invalid telemetry overhead observation")
            loss = 1 - on["goodput"] / off["goodput"]
            inflation = on["p95Seconds"] / off["p95Seconds"] - 1
            # Ratios and subtraction can move an exact 5% boundary by a few ULPs.
            roundoff = 8 * math.ulp(1.0)
            passed = loss <= 0.05 + roundoff and inflation <= 0.05 + roundoff
            cells[key] = {
                "goodputLoss": loss,
                "p95Increase": inflation,
                "passed": passed,
            }
            qualified &= passed
        results.append({"pair": index, "cells": cells})
    return {"qualified": qualified, "threshold": 0.05, "pairs": results}


def resource_scope(settings) -> str:
    """Only resource/input/collector scope: calibration results are not inputs."""
    value = {
        "nodes": [node.model_dump(by_alias=True) for node in settings.nodes],
        "functions": {
            name: fn.model_dump(by_alias=True)
            for name, fn in settings.functions.items()
        },
        "logs": settings.observability.logs.fingerprint(),
        "strictMemory": settings.strict_memory,
        "runtimeDistributionSha256": settings.runtime_distribution.sha256
        if settings.runtime_distribution
        else None,
    }
    if settings.host_cpu_placement is not None:
        value["hostCpuPlacement"] = settings.host_cpu_placement.model_dump(
            by_alias=True
        )
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def require_resource_overhead(settings) -> dict | None:
    """Fail before VM acquisition if the collector has not passed its physical gate."""
    if not (
        settings.strict_memory
        and len(settings.functions) > 1
        and settings.purpose == "scientific-experiment"
    ):
        return None
    reference = settings.observability.vm_resource_overhead
    if reference is None:
        raise ValueError(
            "whole-VM telemetry overhead qualification is required before measurements"
        )
    value = json.loads(reference.read_verified())
    policy = value.get("policy")
    if not isinstance(policy, dict) or "samplerSha256" in policy:
        raise ValueError(
            "resource telemetry overhead qualification predates the workbench "
            "(VM sampler policy): re-qualify it with the workbench exporters"
        )
    policy = {key: item for key, item in policy.items() if key != "exporterImages"}
    if policy != telemetry_policy():
        raise ValueError("resource telemetry overhead export policy changed")
    if value.get("nanolabPackageSha256") != package_fingerprint():
        raise ValueError("resource telemetry overhead collector scope changed")
    if value.get("resourceScopeSha256") != resource_scope(settings):
        raise ValueError(
            "resource telemetry overhead runtime/input/resource scope changed"
        )
    cells = {
        node.id + "/" + name for node in settings.nodes for name in settings.functions
    }

    receipts = value.get("rawEvidence")
    if not isinstance(receipts, list) or len(receipts) != 3:
        raise ValueError("resource overhead lacks three paired raw evidence references")
    for index, pair in enumerate(receipts):
        for side in ["off", "on"]:
            receipt = json.loads(
                ArtifactReference.model_validate(pair[side]).read_verified()
            )
            if receipt.get("byNodeFunction") != value["rawPairs"][index][side]:
                raise ValueError("resource overhead raw node/function evidence differs")
            if not receipt.get("physicalCompletions") or not receipt.get(
                "nativeEpochs"
            ):
                raise ValueError("resource overhead lacks actual physical/native load")
            if side == "on" and receipt.get("resourceCoverageComplete") is not True:
                raise ValueError(
                    "resource overhead telemetry raw coverage is incomplete"
                )
    observed = qualify_overhead(value["rawPairs"], expected_cells=cells)
    if not observed["qualified"] or value.get("qualified") is not True:
        raise ValueError("resource telemetry overhead is NOT_QUALIFIED")
    if (
        value.get("method") != "paired-whole-vm-telemetry-v1"
        or value.get("loggingEnabledBothSides") is not True
        or value.get("rawEvidenceVerified") is not True
    ):
        raise ValueError("resource telemetry overhead lacks verified physical trials")
    return value


def capacity_telemetry_enabled(settings) -> bool:
    """Keep physical trial instrumentation identical after its gate is qualified."""
    return (
        isinstance(settings, OneShotConfig)
        and settings.strict_memory
        and len(settings.functions) > 1
        and settings.observability.vm_resource_overhead is not None
    )
