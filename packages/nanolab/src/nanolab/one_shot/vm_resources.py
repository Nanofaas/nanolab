"""Owned VM telemetry streams, frozen policy and raw coverage accounting."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import uuid4

from sonata_engine import Resource

from nanolab.one_shot import vm_sampler
from nanolab.tasks.one_shot.artifacts import canonical_bytes, write_immutable_artifact


def telemetry_policy() -> dict:
    """Bind sampler implementation and explicit units into the campaign freeze."""
    return {
        "schemaVersion": 1,
        "periodSeconds": 1,
        "scope": "whole-vm-and-all-running-docker-cgroups-v2",
        "cpuUnit": "fraction-of-vcpu-capacity",
        "memoryUnit": "bytes",
        "warmupIncluded": True,
        "transport": "multipass-pipes-with-owned-drain-threads",
        "samplerSha256": hashlib.sha256(
            Path(vm_sampler.__file__).read_bytes()
        ).hexdigest(),
    }


correlate_sample = vm_sampler.correlate_sample


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
    """Count every expected one-second slot, including nodes with no raw file."""
    result: dict[str, Any] = {
        "policy": telemetry_policy(),
        "complete": True,
        "nodes": {},
    }
    expected = set(range(-warmup, period * epochs))
    for node in nodes:
        path = run_dir / "vm-resources" / (node + ".jsonl")
        rows, malformed = read_samples(path)
        counts: dict[int, int] = {}
        errors = []
        container_errors = []
        missing_containers = []
        invalid = []
        roles = set()
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
                )
                slot = int(
                    (
                        datetime.fromisoformat(row["timestampUtc"]) - anchor
                    ).total_seconds()
                    // 1
                )
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
        )
        result["nodes"][node] = {
            "complete": complete,
            "expectedSamples": len(expected),
            "observedSeconds": len(counts),
            "missingSeconds": missing,
            "duplicateSeconds": duplicates,
            "unavailableCpuSeconds": invalid,
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


class VmTelemetrySession:
    """Stream VM-local samples to owned host files before generator acquisition."""

    def __init__(self, run, inputs):
        """Stage frozen sampler bytes and own every started transport."""
        self.run, self.inputs = run, inputs
        self.sessions: list[dict] = []
        self.closed = False
        directory = run.run_dir / "vm-resources"
        directory.mkdir(parents=True, exist_ok=True)
        manifest = json.loads((run.run_dir / "manifest.json").read_text())
        context = {
            "anchor": manifest["anchor"],
            "warmup": manifest["parameters"]["generator"]["warmupSeconds"],
            "period": int(manifest["period_seconds"]),
            "epochs": len(run.trace.windows),
            "mode": run.mode,
            "functions": list(run.settings.functions),
            "run_id": manifest["run_id"],
        }
        try:
            for node in run.topology.resources.nodes.values():
                vm = inputs.resource(node.vm)
                remote = vm.home + "/nanolab-vm-sampler-" + uuid4().hex + ".py"
                staged = node.provider.transfer_to(
                    node.request, source=Path(vm_sampler.__file__), destination=remote
                )
                if staged.return_code:
                    raise RuntimeError("VM sampler transfer failed")
                actual = node.command(("sha256sum", remote)).stdout.split()[0]
                if actual != telemetry_policy()["samplerSha256"]:
                    raise ValueError("VM sampler hash mismatch")
                if not node.request.name:
                    raise ValueError("VM sampler requires owned VM name")
                output = (directory / (node.config.id + ".jsonl")).open(
                    "xb", buffering=0
                )
                errors = (directory / (node.config.id + ".stderr")).open(
                    "xb", buffering=0
                )
                session = {
                    "node": node,
                    "remote": remote,
                    "output": output,
                    "errors": errors,
                    "process": None,
                    "readers": [],
                    "readerErrors": [],
                }
                self.sessions.append(session)
                process = subprocess.Popen(
                    (
                        "multipass",
                        "exec",
                        node.request.name,
                        "--",
                        "sudo",
                        "python3",
                        "-u",
                        remote,
                        "--node",
                        node.config.id,
                        "--duration",
                        str(
                            720
                            + len(run.trace.windows) * run.trace.period_seconds
                            + 180
                        ),
                        "--context",
                        json.dumps(context),
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                session["process"] = process
                # Multipass requires pollable descriptors; regular output files
                # hang its transport. Drain both pipes concurrently into raw files.
                for source, destination in (
                    (process.stdout, output),
                    (process.stderr, errors),
                ):
                    reader = Thread(
                        target=self._drain,
                        args=(source, destination, session["readerErrors"]),
                        daemon=True,
                    )
                    session["readers"].append(reader)
                    reader.start()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                self.require_healthy()
                if all(
                    read_samples(directory / (s["node"].config.id + ".jsonl"))[0]
                    for s in self.sessions
                ):
                    break
                time.sleep(0.1)
            else:
                raise TimeoutError(
                    "VM telemetry startup did not produce all node samples"
                )
            write_immutable_artifact(
                directory / "policy.json", canonical_bytes(telemetry_policy())
            )
        except BaseException as error:
            try:
                self.close()
            except Exception as cleanup:
                error.add_note("VM telemetry startup cleanup: " + str(cleanup))
            raise

    @staticmethod
    def _drain(source, destination, failures):
        """Preserve every byte, including partial lines, as soon as it arrives."""
        try:
            with source:
                while chunk := source.read1(65536):
                    destination.write(chunk)
        except Exception as error:
            failures.append(str(error))

    def require_healthy(self):
        """Treat terminated or stalled streams as visible collection failures."""
        for session in self.sessions:
            process = session["process"]
            path = Path(session["output"].name)
            if session["readerErrors"]:
                raise RuntimeError(
                    "VM telemetry raw write failed: "
                    + "; ".join(session["readerErrors"])
                )
            if process is not None and process.poll() is not None:
                raise RuntimeError("VM telemetry stopped: " + session["node"].config.id)
            if path.stat().st_size and time.time() - path.stat().st_mtime > 10:
                raise RuntimeError("VM telemetry stalled: " + session["node"].config.id)

    def close(self, *, coverage_until: datetime | None = None):
        """Reap transports and sync partial raw before releasing any VM resource."""
        if self.closed:
            return
        failures = []
        for session in self.sessions:
            process = session["process"]
            try:
                # The unique uploaded script is the ownership token; only its PID
                # is signalled. Remote cleanup completes before closing transport.
                node = session["node"]
                node.command(("sudo", "python3", session["remote"], "--stop"))
                if process is not None:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise RuntimeError(
                            "remote VM telemetry stop unconfirmed"
                        ) from None
            except Exception as error:
                failures.append(str(error))
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                elif process is not None:
                    process.wait(timeout=5)
            finally:
                # Process reaping produces EOF; readers must finish before fsync
                # or closing raw files, otherwise terminal samples can be lost.
                for reader in session["readers"]:
                    reader.join(timeout=5)
                    if reader.is_alive():
                        failures.append("VM telemetry pipe drain did not stop")
                failures.extend(session["readerErrors"])
                for stream in (session["output"], session["errors"]):
                    try:
                        os.fsync(stream.fileno())
                    except OSError as error:
                        failures.append(str(error))
                    finally:
                        stream.close()
        self.closed = True
        manifest_path = self.run.run_dir / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            generator = manifest["parameters"]["generator"]
            expected_period = int(manifest["period_seconds"])
            if coverage_until is not None:
                expected_period = max(
                    0,
                    min(
                        expected_period,
                        int(
                            (
                                coverage_until
                                - datetime.fromisoformat(manifest["anchor"])
                            ).total_seconds()
                        ),
                    ),
                )
            coverage = summarize_resources(
                self.run.run_dir,
                anchor=datetime.fromisoformat(manifest["anchor"]),
                warmup=generator["warmupSeconds"],
                period=expected_period,
                epochs=len(self.run.trace.windows),
                nodes=list(self.run.topology.resources.nodes),
                mode=self.run.mode,
                functions=list(self.run.settings.functions),
                run_id=manifest["run_id"],
            )
            coverage["measuredThroughUtc"] = (
                coverage_until.isoformat() if coverage_until else None
            )
            coverage["cleanupErrors"] = failures
            write_immutable_artifact(
                self.run.run_dir / "vm-resources" / "coverage.json",
                canonical_bytes(coverage),
            )
        if failures:
            raise RuntimeError("VM telemetry cleanup failures: " + "; ".join(failures))


def vm_telemetry_resource(run) -> Resource:
    """Reuse the owned topology lifetime and preserve legacy non-Multipass runs."""
    return Resource(
        title="Own continuous whole-VM and container telemetry",
        acquire=lambda inputs: VmTelemetrySession(run, inputs),
        release=lambda _inputs, session: session.close(),
        requires=run.topology.requires,
    )


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
            passed = loss <= 0.05 and inflation <= 0.05
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
    from nanolab.tasks.one_shot.artifacts import package_fingerprint

    if (
        value.get("policy") != telemetry_policy()
        or value.get("nanolabPackageSha256") != package_fingerprint()
    ):
        raise ValueError("resource telemetry overhead collector scope changed")
    if value.get("resourceScopeSha256") != resource_scope(settings):
        raise ValueError(
            "resource telemetry overhead runtime/input/resource scope changed"
        )
    cells = {
        node.id + "/" + name for node in settings.nodes for name in settings.functions
    }
    from nanolab.config.one_shot import ArtifactReference

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
    from nanolab.config.one_shot import OneShotConfig

    return (
        isinstance(settings, OneShotConfig)
        and settings.strict_memory
        and len(settings.functions) > 1
        and settings.observability.vm_resource_overhead is not None
    )
