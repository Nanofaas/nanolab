"""Owned open-loop capacity trials reuse the campaign generator and SDK collector."""

from __future__ import annotations

import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx

from nanolab.tasks.one_shot.artifacts import canonical_bytes, write_immutable_artifact
from nanolab.tasks.one_shot.trace import TraceArtifact, request_schedule


def run_capacity_trial(
    topology,
    settings,
    inputs,
    value,
    *,
    replicas: dict[str, int],
    rates: dict[str, float],
    repetition: int,
    run_dir: Path,
    clock,
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

    run_dir.mkdir(parents=True, exist_ok=False)
    function_name = next(iter(topology.function_settings))
    payload = topology.function_settings[function_name].input
    expected = value["outputs"][function_name]
    runtime_urls = {}
    with httpx.Client(trust_env=False) as http:
        for node_id, count in replicas.items():
            response = http.put(
                inputs.resource(topology.endpoints[node_id])
                + f"/v1/functions/{function_name}/replicas",
                json={"replicas": count},
                timeout=30,
            )
            response.raise_for_status()
            runtime_urls[node_id] = []
            for replica in range(1, count + 1):
                deadline = time.monotonic() + 30
                while True:
                    try:
                        runtime, _ = runtime_endpoint(
                            topology.resources.nodes[node_id],
                            function_name,
                            inputs=inputs,
                            replica=replica,
                        )
                        status = http.get(runtime + "/runtime/status", timeout=2)
                        status.raise_for_status()
                        if (
                            status.json().get("maxConcurrentHandlers") != 1
                            or status.json().get("physicalReleaseProof") is not True
                        ):
                            raise ValueError("capacity replica lacks physical c1 proof")
                        break
                    except (httpx.HTTPError, RuntimeError, ValueError):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.1)
                runtime_urls[node_id].append(runtime)
                warm = measure_invocation(
                    http,
                    runtime,
                    payload=payload,
                    execution_id=f"capacity-warm-{node_id}-{replica}-{run_dir.name}",
                    expected_output=expected,
                    timeout_seconds=settings.calibration.timeout_seconds,
                    release_timeout_seconds=settings.calibration.timeout_seconds,
                )
                if warm["state"] != "complete":
                    raise ValueError("capacity replica warmup failed")
    effective = {node: round(rate * 90) / 90 for node, rate in rates.items()}
    if any(rate <= 0 for rate in effective.values()):
        raise ValueError("capacity arrival grid loses positive load")
    anchor = datetime.now(UTC) + timedelta(seconds=2)
    trace = TraceArtifact(
        nodes=list(effective),
        functions=[function_name],
        period_seconds=90,
        flow_quantum=1 / 90,
        seed=settings.seed,
        payloads={function_name: payload},
        windows=[{node: {function_name: rate} for node, rate in effective.items()}],
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
        replica = int(row["originalId"].rsplit(":", 1)[1]) % replicas[node]
        row.update(sdk=True, url=runtime_urls[node][replica] + "/invoke")
    write_immutable_artifact(run_dir / "schedule.json", canonical_bytes(schedule))
    run = SimpleNamespace(topology=topology, run_dir=run_dir)
    resource = generator_resource(run_dir=run_dir, vus=512, duration=90)
    primary = None
    try:
        with ExitStack() as stack:
            process = resource.acquire(inputs)
            stack.callback(resource.release, inputs, process)
            collector = LiveEvidenceCollector(run, inputs)
            stack.callback(collector.close)
            deadline = time.monotonic() + 150
            while process.poll() is None:
                inputs.resource(clock).require_healthy()
                collector.require_healthy()
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
            (run_dir / "failure.txt").write_text(str(primary) + "\n")
    rows = generator_rows(run_dir / "generator.jsonl")
    proofs = physical_attempts(run_dir / "physical.jsonl")
    measurements = []
    for node in effective:
        planned = {
            row["originalId"]: row
            for row in schedule
            if row["origin"] == node and row["scheduledAt"] >= anchor.timestamp() + 30
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
            physical = [
                proof
                for proof in local_proofs
                if proof["originalId"] == row["originalId"]
            ]
            result = classify_result(row, expected=str(expected), physical=physical)
            valid_output &= (
                row["statusCode"] != 200 or result["success"] or result["terminalError"]
            )
            if (
                result["success"]
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
            "replicas": replicas[node],
            "rate": effective[node],
            "requestedRate": rates[node],
            "repetition": repetition,
            "warmupSeconds": 30,
            "measurementSeconds": 60,
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
            "relativeServiceDrift": abs(sum(durations) / len(durations) / service - 1)
            if durations
            else 1.0,
            "maxArrivalLatenessSeconds": max(lateness) if lateness else None,
            "trialDirectory": str(run_dir),
        }
        measurements.append(measured)
    write_immutable_artifact(run_dir / "trial.json", canonical_bytes(measurements))
    return measurements
