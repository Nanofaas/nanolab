"""Owned open-loop capacity trials reuse the campaign generator and SDK collector."""

from __future__ import annotations

import math
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from nanolab.config.one_shot import resolve_node_functions
from nanolab.one_shot.vm_resources import VmTelemetrySession, capacity_telemetry_enabled
from nanolab.tasks.one_shot.artifacts import (
    append_observation,
    canonical_bytes,
    write_immutable_artifact,
)
from nanolab.tasks.one_shot.trace import TraceArtifact, request_schedule


def _capacity_vectors(topology, settings, functions, replicas, rates, window_seconds):
    """Validate per-node memory and arrival grids before acquiring runtimes."""
    aliases = getattr(topology, "function_aliases", {})
    node_settings, counts, effective = {}, {}, {}
    for node, vector in replicas.items():
        node_config = next(
            (entry for entry in settings.nodes if entry.id == node), None
        )
        if node_config is None or node not in topology.endpoints:
            raise ValueError("unknown capacity node")
        local = {
            aliases.get(name, name): fn
            for name, fn in resolve_node_functions(settings, node).items()
        }
        node_settings[node] = local
        if (
            not isinstance(vector, dict)
            or not isinstance(rates[node], dict)
            or set(vector) - set(functions)
            or set(rates[node]) - set(functions)
        ):
            raise ValueError("capacity function scope mismatch")
        counts[node], effective[node] = {}, {}
        for function in functions:
            count = vector.get(function, 0)
            rate = rates[node].get(function, 0)
            if (
                type(count) is not int
                or count < 0
                or count > local[function].max_replicas
                or isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not math.isfinite(rate)
                or rate < 0
            ):
                raise ValueError("invalid capacity replicas/rate")
            if (count == 0 and rate != 0) or (count > 0 and rate == 0):
                raise ValueError("capacity active replica/rate scope mismatch")
            rounded = round(rate * window_seconds) / window_seconds
            if rate > 0 and rounded <= 0:
                raise ValueError("capacity arrival grid loses positive load")
            counts[node][function], effective[node][function] = count, rounded
        if not any(counts[node].values()):
            raise ValueError("capacity node has no active function")
        if (
            sum(counts[node][name] * local[name].memory_mib for name in functions)
            > node_config.memory_capacity_mib
        ):
            raise ValueError("capacity replica vector exceeds memory pool")
    return node_settings, counts, effective


def _run_capacity_trial(
    topology,
    settings,
    inputs,
    value,
    *,
    replicas: dict,
    rates: dict,
    repetition: int,
    run_dir: Path,
    clock,
    telemetry_enabled: bool | None = None,
    preload_lead_seconds: float | None = None,
    warmup_seconds: int = 30,
    measurement_seconds: int = 60,
) -> list[dict]:
    """Retain every request, reject missing proof, and compensate owned processes."""
    # Imports are local because the public experiment module uses calibration's
    # checksum oracle; this task reuses its existing lifecycle rather than a CLI.
    from nanolab.tasks.one_shot.calibration import measure_invocation
    from nanolab.tasks.one_shot.experiment import (
        LiveEvidenceCollector,
        classify_result,
        collect_runtime_evidence,
        generator_resource,
        generator_rows,
        physical_attempts,
    )
    from nanolab.tasks.one_shot.preflight import runtime_endpoint

    if any(
        type(duration) is not int or duration <= 0
        for duration in (warmup_seconds, measurement_seconds)
    ):
        raise ValueError(
            "capacity warmup and measurement durations must be positive integers"
        )
    window_seconds = warmup_seconds + measurement_seconds
    if preload_lead_seconds is not None and (
        not math.isfinite(preload_lead_seconds) or preload_lead_seconds < 2
    ):
        raise ValueError("capacity pre-load lead must be finite and >=2 seconds")
    run_dir.mkdir(parents=True, exist_ok=False)
    from nanolab.tasks.one_shot.preflight import (
        collect_container_cgroup,
        validate_strict_cgroup,
    )

    functions = list(topology.function_settings)
    legacy = all(type(count) is int for count in replicas.values())
    if legacy:
        if len(functions) != 1:
            raise ValueError("scalar capacity input has ambiguous function scope")
        replicas = {node: {functions[0]: count} for node, count in replicas.items()}
        rates = {node: {functions[0]: rate} for node, rate in rates.items()}
    if not replicas or set(replicas) != set(rates):
        raise ValueError("capacity node scope mismatch")
    node_settings, counts, effective = _capacity_vectors(
        topology, settings, functions, replicas, rates, window_seconds
    )
    runtime_urls = {}
    with httpx.Client(trust_env=False) as http:
        for node_id, vector in counts.items():
            gateway = inputs.resource(topology.endpoints[node_id])
            node = topology.resources.nodes[node_id]
            if not legacy:
                # Drain the previous configuration before growing a new feasible one.
                for name in functions:
                    http.put(
                        gateway + f"/v1/functions/{name}/replicas",
                        json={"replicas": 0},
                        timeout=30,
                    ).raise_for_status()
                    deadline = time.monotonic() + 30
                    while True:
                        remaining = node.command(
                            (
                                "docker",
                                "ps",
                                "-q",
                                "--filter",
                                "label=io.nanofaas.managed=true",
                                "--filter",
                                f"label=io.nanofaas.function={node.request.name}/{name}",
                            )
                        ).stdout.strip()
                        if not remaining:
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "capacity previous replicas did not drain"
                            )
                        time.sleep(0.1)
            runtime_urls[node_id] = {}
            for name, count in vector.items():
                if not count:
                    continue
                payload = topology.function_settings[name].input
                expected = value["outputs"][name]
                http.put(
                    gateway + f"/v1/functions/{name}/replicas",
                    json={"replicas": count},
                    timeout=30,
                ).raise_for_status()
                runtime_urls[node_id][name] = []
                for replica in range(1, count + 1):
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            runtime, observed = runtime_endpoint(
                                node, name, inputs=inputs, replica=replica
                            )
                            status = http.get(runtime + "/runtime/status", timeout=2)
                            status.raise_for_status()
                            if (
                                status.json().get("maxConcurrentHandlers") != 1
                                or status.json().get("physicalReleaseProof") is not True
                            ):
                                raise ValueError(
                                    "capacity replica lacks physical c1 proof"
                                )
                            break
                        except (httpx.HTTPError, RuntimeError, ValueError):
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.1)
                    runtime_urls[node_id][name].append(runtime)
                    if settings.strict_memory:
                        expected_resources = node_settings[node_id][name]
                        if (
                            observed.get("HostConfig", {}).get("MemorySwap")
                            != expected_resources.memory_mib * 1024 * 1024
                        ):
                            raise ValueError("capacity strict swap limit mismatch")
                        sample = collect_container_cgroup(node, observed)
                        write_immutable_artifact(
                            run_dir / f"{node_id}-{name}-r{replica}-cgroup-before.json",
                            canonical_bytes(sample),
                        )
                        validate_strict_cgroup(
                            sample,
                            memory_mib=expected_resources.memory_mib,
                            cpu=expected_resources.cpu,
                        )
                    warm = measure_invocation(
                        http,
                        runtime,
                        payload=payload,
                        execution_id=f"capacity-warm-{node_id}-{name}-{replica}-{run_dir.name}",
                        expected_output=expected,
                        timeout_seconds=settings.calibration.timeout_seconds,
                        release_timeout_seconds=settings.calibration.timeout_seconds,
                    )
                    if warm["state"] != "complete":
                        raise ValueError("capacity replica warmup failed")

    use_telemetry = (
        capacity_telemetry_enabled(settings)
        if telemetry_enabled is None
        else telemetry_enabled
    )
    lead = (
        preload_lead_seconds
        if preload_lead_seconds is not None
        else (45 if use_telemetry else 2)
    )
    extra_budget = lead if lead > 2 else 0
    captured_at = datetime.now(UTC)
    captured_mono = time.monotonic()
    anchor = captured_at + timedelta(seconds=lead)
    parameters: dict[str, Any] = {"generator": {"warmupSeconds": warmup_seconds}}
    monitor = None
    if settings.protocol_version == 2:
        monitor = inputs.resource(clock)
        probe_directory = monitor.probe_directory
        if (
            probe_directory is None
            or probe_directory.resolve() not in run_dir.resolve().parents
        ):
            raise ValueError("capacity clock probes must belong to a workflow ancestor")
        parameters["logicalSchedule"] = {
            "protocolVersion": 2,
            "anchorMonotonicSeconds": captured_mono + lead + warmup_seconds,
            "capturedMonotonicSeconds": captured_mono,
            "capturedAtUtc": captured_at.isoformat(),
            "hostBootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "probeScope": "parent",
            "probeParentLevels": run_dir.resolve().parents.index(
                probe_directory.resolve()
            )
            + 1,
        }
    trace = TraceArtifact(
        nodes=list(effective),
        functions=functions,
        period_seconds=window_seconds,
        flow_quantum=1 / window_seconds,
        seed=settings.seed,
        payloads={name: topology.function_settings[name].input for name in functions},
        windows=[effective],
    )
    schedule = request_schedule(
        trace,
        anchor=anchor,
        endpoints={
            node: inputs.resource(topology.endpoints[node]) for node in effective
        },
        run_id="capacity:" + run_dir.name,
    )
    for row in schedule:
        node = row["origin"]
        name = row["function"]
        replica = int(row["originalId"].rsplit(":", 1)[1]) % counts[node][name]
        row.update(sdk=True, url=runtime_urls[node][name][replica] + "/invoke")
    write_immutable_artifact(run_dir / "schedule.json", canonical_bytes(schedule))
    write_immutable_artifact(
        run_dir / "manifest.json",
        canonical_bytes(
            {
                "purpose": "capacity-or-observer-only",
                "anchor": (anchor + timedelta(seconds=warmup_seconds)).isoformat(),
                "period_seconds": measurement_seconds,
                "run_id": "capacity:" + run_dir.name,
                "parameters": parameters,
            }
        ),
    )
    run = SimpleNamespace(
        topology=topology,
        run_dir=run_dir,
        runtime_replica_counts=counts,
        settings=settings,
        trace=trace,
        mode="capacity",
        sdk_request_targets={
            row["originalId"]: (row["origin"], row["function"], row["url"])
            for row in schedule
        },
    )
    resource = generator_resource(
        run_dir=run_dir, vus=512, duration=window_seconds + extra_budget
    )
    primary = None

    def compensate(callback, *args):
        original = sys.exception()
        try:
            callback(*args)
        except Exception as error:
            if original is None:
                raise
            original.add_note(f"cleanup also failed: {error}")
            try:
                append_observation(
                    run_dir / "cleanup-errors.jsonl", {"error": str(error)}
                )
            except OSError as evidence_error:
                original.add_note(f"cleanup diagnostic unavailable: {evidence_error}")

    try:
        with ExitStack() as stack:
            telemetry = None
            if use_telemetry:
                # The sampler starts before the same physical SDK load path.
                telemetry = VmTelemetrySession(
                    run, inputs, terminal_probe=monitor.refresh if monitor else None
                )
                stack.callback(compensate, telemetry.close)
            process = resource.acquire(inputs)
            stack.callback(compensate, resource.release, inputs, process)
            collector = LiveEvidenceCollector(run, inputs)
            stack.callback(compensate, collector.close)
            deadline = time.monotonic() + window_seconds + 60 + extra_budget
            while process.poll() is None:
                inputs.resource(clock).require_healthy()
                collector.require_healthy()
                if telemetry is not None:
                    telemetry.require_healthy()
                if time.monotonic() >= deadline:
                    raise RuntimeError("capacity generator exceeded bounded drain")
                time.sleep(0.25)
            if process.returncode != 0:
                raise RuntimeError("capacity generator failed")
            collector.close()
        collect_runtime_evidence(
            run, inputs, generator_rows(run_dir / "generator.jsonl")
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        # Record the original error even if collection or compensation failed.
        if primary is not None:
            try:
                (run_dir / "failure.txt").write_text(str(primary) + "\n")
            except OSError as error:
                primary.add_note(f"failure diagnostic unavailable: {error}")
    rows = generator_rows(run_dir / "generator.jsonl")
    proofs = physical_attempts(run_dir / "physical.jsonl")
    proof_by_id = {}
    for proof in proofs:
        proof_by_id.setdefault(proof["originalId"], []).append(proof)
    measurements = []
    for node, vector in counts.items():
        for function_name, replica_count in vector.items():
            if not replica_count:
                continue
            expected = value["outputs"][function_name]
            planned = {
                row["originalId"]: row
                for row in schedule
                if row["origin"] == node
                and row["function"] == function_name
                and row["scheduledAt"] >= anchor.timestamp() + warmup_seconds
            }
            emitted = [
                row
                for row in rows
                if row["event"] == "emitted" and row["originalId"] in planned
            ]
            outcomes = [
                row
                for row in rows
                if row["event"] == "result" and row["originalId"] in planned
            ]
            ids = {row["originalId"] for row in emitted}
            local_proofs = [proof for proof in proofs if proof["originalId"] in planned]
            completed, errors, durations, valid_output = set(), set(), [], True
            for row in outcomes:
                physical = proof_by_id.get(row["originalId"], [])
                result = classify_result(row, expected=str(expected), physical=physical)
                if any(proof.get("function") != function_name for proof in physical):
                    valid_output = False
                    continue
                valid_output &= (
                    row["statusCode"] != 200
                    or result["success"]
                    or result["terminalError"]
                )
                if (
                    result["success"]
                    and row["originalId"] in ids
                    and len(physical) == 1
                    and physical[0].get("state") == "RELEASED"
                    and physical[0].get("handlerStarted")
                    and physical[0]["destination"] == node
                ):
                    completed.add(row["originalId"])
                    durations.append(physical[0]["occupancySeconds"])
                elif result["terminalError"]:
                    errors.add(row["originalId"])
            lateness = [
                row["emittedAt"] - planned[row["originalId"]]["scheduledAt"]
                for row in emitted
            ]
            duplicate = (
                len(emitted) != len(ids)
                or len(outcomes) != len({row["originalId"] for row in outcomes})
                or len(local_proofs) > len(completed)
            )
            deficit = len(set(planned) - ids)
            service = value["statistics"][f"{node}/{function_name}"]["meanSeconds"]
            measured = {
                "node": node,
                "replicas": replica_count,
                "rate": effective[node][function_name],
                "requestedRate": rates[node].get(function_name, 0),
                "repetition": repetition,
                "warmupSeconds": warmup_seconds,
                "measurementSeconds": measurement_seconds,
                "planned": len(planned),
                "emitted": len(emitted),
                "physicalCompletions": len(completed),
                "knownErrors": len(errors),
                "censored": len(ids - completed - errors),
                "generatorDeficit": deficit,
                "validOutput": bool(
                    valid_output
                    and not duplicate
                    and lateness
                    and min(lateness) >= -0.001
                    and max(lateness) <= 0.25
                ),
                "relativeServiceDrift": abs(
                    sum(durations) / len(durations) / service - 1
                )
                if durations
                else 1.0,
                "maxArrivalLatenessSeconds": max(lateness) if lateness else None,
                "trialDirectory": str(run_dir),
            }
            if not legacy:
                measured.update(
                    function=function_name, replicaVector=vector, mix=effective[node]
                )
            measurements.append(measured)
    if settings.strict_memory:
        for node_id, vector in counts.items():
            for name, count in vector.items():
                for replica in range(1, count + 1):
                    node = topology.resources.nodes[node_id]
                    _, observed = runtime_endpoint(
                        node, name, inputs=inputs, replica=replica
                    )
                    sample = collect_container_cgroup(node, observed)
                    write_immutable_artifact(
                        run_dir / f"{node_id}-{name}-r{replica}-cgroup-after.json",
                        canonical_bytes(sample),
                    )
                    expected_resources = node_settings[node_id][name]
                    validate_strict_cgroup(
                        sample,
                        memory_mib=expected_resources.memory_mib,
                        cpu=expected_resources.cpu,
                    )
    write_immutable_artifact(run_dir / "trial.json", canonical_bytes(measurements))
    return measurements


def run_capacity_trial(
    topology,
    settings,
    inputs,
    value,
    *,
    replicas: dict,
    rates: dict,
    repetition: int,
    run_dir: Path,
    clock,
    telemetry_enabled: bool | None = None,
    preload_lead_seconds: float | None = None,
    warmup_seconds: int = 30,
    measurement_seconds: int = 60,
) -> list[dict]:
    """Keep the original error even during allocation, warmup or cgroup collection."""
    try:
        return _run_capacity_trial(
            topology,
            settings,
            inputs,
            value,
            replicas=replicas,
            rates=rates,
            repetition=repetition,
            run_dir=run_dir,
            clock=clock,
            telemetry_enabled=telemetry_enabled,
            preload_lead_seconds=preload_lead_seconds,
            warmup_seconds=warmup_seconds,
            measurement_seconds=measurement_seconds,
        )
    except BaseException as error:
        if run_dir.is_dir() and not (run_dir / "failure.txt").exists():
            try:
                (run_dir / "failure.txt").write_text(str(error) + "\n")
            except OSError as evidence_error:
                error.add_note(f"failure diagnostic unavailable: {evidence_error}")
        raise
