"""Independent warm physical-occupancy measurement under Sonata ownership."""

from __future__ import annotations

import math
import platform
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome

from nanolab.config.one_shot import OneShotConfig, resolve_node_functions
from nanolab.one_shot.capacity import (
    CAPACITY_FACTORS,
    capacity_configurations,
    select_capacity,
    select_mixed_capacity,
)
from nanolab.one_shot.models import CalibrationProfile
from nanolab.one_shot.profiles import CalibrationProfiles, ProfileSet
from nanolab.tasks.one_shot.artifacts import (
    append_observation,
    canonical_bytes,
    content_hash,
    package_fingerprint,
    write_immutable_artifact,
)
from nanolab.tasks.one_shot.capacity import run_capacity_trial
from nanolab.tasks.one_shot.clock import ClockMonitor
from nanolab.tasks.one_shot.preflight import TopologyEvidence, runtime_endpoint
from nanolab.tasks.one_shot.statistics import summarize_service


def workload_checksum(
    payload: dict[str, Any], *, function: str = "one-shot-workload"
) -> int:
    """Compute the independent integer oracle of the bounded Rust CPU/memory work."""
    if function.removesuffix("-rust") != "one-shot-workload":
        from nanolab.tasks.one_shot.workload import expected_output

        return expected_output(function, payload)
    if set(payload) != {"iterations", "working_set_bytes", "seed"} or any(
        type(value) is not int or value < 0 for value in payload.values()
    ):
        raise ValueError("one-shot workload requires unsigned integer input")
    iterations, size, seed = (
        payload["iterations"],
        payload["working_set_bytes"],
        payload["seed"],
    )
    if iterations > 1_000_000_000 or size > 128 * 1024 * 1024 or seed >= 1 << 64:
        raise ValueError("one-shot workload input exceeds runtime bounds")
    mask = (1 << 64) - 1
    memory = bytearray((seed + index) & 255 for index in range(size))
    checksum = seed
    for index in range(iterations):
        checksum = (
            checksum * 6364136223846793005 + (index ^ 1442695040888963407)
        ) & mask
        if size:
            position = checksum % size
            memory[position] = (memory[position] + (checksum >> 32)) & 255
            checksum ^= memory[position]
    for byte in memory:
        checksum = ((checksum << 5) | (checksum >> 59)) & mask
        checksum ^= byte
    return checksum


def measure_invocation(
    http: httpx.Client,
    runtime: str,
    *,
    payload: dict[str, Any],
    execution_id: str,
    expected_output: Any,
    timeout_seconds: float,
    release_timeout_seconds: float,
) -> dict[str, Any]:
    """Record physical release, keeping HTTP timeout and missing proof explicit."""
    sample: dict[str, Any] = {
        "executionId": execution_id,
        "state": "censored",
        "occupancySeconds": None,
        "httpSeconds": None,
        "runtime": runtime,
        "callerTimedOut": False,
    }
    started = time.monotonic()
    valid_response = False
    try:
        response = http.post(
            runtime + "/invoke",
            json={"input": payload},
            headers={"X-Execution-Id": execution_id, "X-Dispatch-Attempt": "1"},
            timeout=timeout_seconds,
        )
        sample["httpStatus"] = response.status_code
        response.raise_for_status()
        body = response.json()
        valid_response = type(body) is type(expected_output) and body == expected_output
        sample["outputVerified"] = valid_response
    except httpx.TimeoutException as error:
        sample["callerTimedOut"], sample["error"] = True, str(error)
    except (httpx.HTTPError, ValueError) as error:
        sample["error"] = str(error)
    sample["httpSeconds"] = time.monotonic() - started
    deadline = time.monotonic() + release_timeout_seconds
    while True:
        try:
            receipt = http.get(
                runtime + "/runtime/executions/" + execution_id,
                timeout=min(timeout_seconds, release_timeout_seconds),
            )
            if receipt.status_code == 404:
                sample["state"] = "missing"
                return sample
            receipt.raise_for_status()
            proof = receipt.json()
            sample["proof"] = proof
            if proof.get("executionId") != execution_id or not proof.get("incarnation"):
                sample["state"] = "error"
                return sample
            if proof.get("state") == "RELEASED":
                duration = proof.get("occupancySeconds")
                if (
                    proof.get("handlerStarted") is not True
                    or proof.get("dispatchAttempt") != "1"
                    or not isinstance(duration, (float, int))
                    or not math.isfinite(duration)
                    or duration <= 0
                ):
                    sample["state"] = "error"
                    return sample
                sample["occupancySeconds"] = duration
                sample["state"] = (
                    "censored"
                    if sample["callerTimedOut"]
                    else "complete"
                    if valid_response
                    else "error"
                )
                return sample
        except (httpx.HTTPError, ValueError) as error:
            sample["proofError"] = str(error)
        if time.monotonic() >= deadline:
            return sample
        time.sleep(min(0.01, release_timeout_seconds))


def environment_identity(
    settings: OneShotConfig,
    topology: TopologyEvidence,
    built: Any,
) -> tuple[str, dict[str, str]]:
    """Bind stable hardware/configuration/image facts, excluding run names and IPs."""
    cpu_snapshot = Path("/proc/cpuinfo").read_bytes()
    stable_keys = {
        "processor",
        "vendor_id",
        "cpu family",
        "model",
        "model name",
        "stepping",
        "microcode",
        "cpu cores",
        "siblings",
        "core id",
        "physical id",
        "cache size",
        "address sizes",
        "flags",
        "features",
        "cpu implementer",
        "cpu architecture",
        "cpu variant",
        "cpu part",
        "cpu revision",
    }
    records = []
    for block in cpu_snapshot.decode().split("\n\n"):
        record = {}
        for line in block.splitlines():
            key, separator, value = line.partition(":")
            key = key.strip().lower()
            if separator and key in stable_keys:
                record[key] = value.strip()
        if record:
            records.append(record)
    if not records:
        raise ValueError("no stable CPU identity/topology fields")
    cpu_info = canonical_bytes(records)
    if any(not node.os or not node.architecture for node in topology.nodes):
        raise ValueError("missing measured VM OS/architecture fingerprint")
    environment = {
        "host": platform.node(),
        "vm": "multipass",
        "os": platform.platform(),
        "architecture": platform.machine(),
        "cpu": "sha256:" + content_hash(cpu_info),
    }
    identity = {
        "provider": settings.provider,
        "purpose": settings.purpose,
        "environment": environment,
        "source": built.source,
        "recipe": built.recipe_sha256,
        "images": {
            component.name: component.image.id for component in built.components
        },
        "nodes": [node.model_dump(by_alias=True) for node in settings.nodes],
        "measuredNodes": [
            {
                "id": node.node_id,
                "cpus": node.cpus,
                "memoryMiB": node.memory_mib,
                "backend": node.backend,
                "concurrency": node.concurrency,
                "os": node.os,
                "architecture": node.architecture,
            }
            for node in topology.nodes
        ],
        "functions": {
            name: function.model_dump(by_alias=True)
            for name, function in settings.functions.items()
        },
        "flowQuantum": settings.flow_quantum,
    }
    if settings.strict_memory:
        identity["strictMemory"] = True
    if settings.purpose == "scientific-experiment":
        identity["nanolabPackageSha256"] = package_fingerprint()
        identity["controlPlaneLimits"] = {"cpu": 0.35, "memoryMiB": 512}
    if settings.neighbors is not None:
        identity["neighbors"] = settings.neighbors
    if settings.observability.logs.enabled:
        if not topology.logging or set(topology.logging) != {
            node.id for node in settings.nodes
        }:
            raise ValueError("missing collector identities for logging fingerprint")
        identity["observability"] = {
            "configuration": settings.observability.logs.fingerprint(),
            "collectors": topology.logging,
        }
    fingerprint = "sha256:" + content_hash(canonical_bytes(identity))
    return fingerprint, environment


def node_environment_identity(
    settings, topology, built, node_id: str
) -> tuple[str, dict[str, str]]:
    """Bind each native profile to its own stable measured VM identity and limits."""
    global_fingerprint, environment = environment_identity(settings, topology, built)
    node = next(row for row in topology.nodes if row.node_id == node_id)
    identity = {
        "campaign": global_fingerprint,
        "node": node_id,
        "cpus": node.cpus,
        "memoryMiB": node.memory_mib,
        "os": node.os,
        "architecture": node.architecture,
        "limits": {
            name: fn.model_dump(by_alias=True)
            for name, fn in resolve_node_functions(settings, node_id).items()
        },
    }
    return "sha256:" + content_hash(canonical_bytes(identity)), {
        **environment,
        "vm": "multipass/" + node_id,
        "os": node.os,
        "architecture": node.architecture,
    }


class _CalibrationTask[T](Task[T]):
    def __init__(
        self,
        topology: Any,
        settings: OneShotConfig,
        run_dir: Path,
        *,
        clock: Resource[ClockMonitor],
    ):
        """Bind the acquired topology and declared warmup conditions."""
        self.topology, self.settings, self.run_dir = topology, settings, run_dir
        self.clock = clock


class WarmupTask(_CalibrationTask[dict[str, Any]]):
    """Verify deterministic CPU/memory output before retaining warm measurements."""

    title = "Warm and verify one-shot calibration workloads"

    def run(self, inputs: TaskInputs) -> TaskOutcome[dict[str, Any]]:
        """Record readiness separately and warm each node/function at physical c1."""
        preflight = inputs.upstream()
        built = inputs.resource(self.topology.distribution)
        fingerprint, environment = environment_identity(self.settings, preflight, built)
        write_immutable_artifact(
            self.run_dir / "cpu-runtime-observations.json",
            canonical_bytes(
                {
                    "observedAt": datetime.now(UTC).isoformat(),
                    "rawCpuInfo": Path("/proc/cpuinfo").read_text(),
                }
            ),
        )
        value: dict[str, Any] = {
            "fingerprint": fingerprint,
            "environment": environment,
            "outputs": {},
            "runtimes": {},
            "samples": {},
            "statistics": {},
            "capacity": [],
            "source": built.source,
        }
        if self.settings.purpose == "scientific-experiment":
            value["nodeFingerprints"], value["nodeEnvironments"] = {}, {}
            for node_id in self.topology.resources.nodes:
                local_fingerprint, local_environment = node_environment_identity(
                    self.settings, preflight, built, node_id
                )
                value["nodeFingerprints"][node_id] = local_fingerprint
                value["nodeEnvironments"][node_id] = local_environment
        with httpx.Client(trust_env=False) as http:
            for name, function in self.topology.function_settings.items():
                output = workload_checksum(function.input, function=name)
                value["outputs"][name] = output
                for key, node in self.topology.resources.nodes.items():
                    started = time.monotonic()
                    runtime, observed = runtime_endpoint(node, name, inputs=inputs)
                    append_observation(
                        self.run_dir / "readiness.jsonl",
                        {
                            "node": key,
                            "function": name,
                            "lookupSeconds": time.monotonic() - started,
                            "containerCreatedAt": observed.get("Created"),
                            "containerStartedAt": observed.get("State", {}).get(
                                "StartedAt"
                            ),
                        },
                    )
                    value["runtimes"][f"{key}/{name}"] = runtime
                    for number in range(self.settings.calibration.warmup_invocations):
                        sample = measure_invocation(
                            http,
                            runtime,
                            payload=function.input,
                            execution_id="warmup-" + uuid4().hex,
                            expected_output=output,
                            timeout_seconds=self.settings.calibration.timeout_seconds,
                            release_timeout_seconds=self.settings.calibration.timeout_seconds,
                        )
                        sample.update(
                            node=key, function=name, phase="warmup", number=number
                        )
                        append_observation(self.run_dir / "warmup.jsonl", sample)
                        if sample["state"] != "complete":
                            raise ValueError("warmup output or physical proof failed")
        return TaskOutcome(value=value)


class MeasureServiceTask(_CalibrationTask[dict[str, Any]]):
    """Collect bounded independent repetitions and preserve every raw observation."""

    title = "Measure warm physical service occupancy"

    def run(self, inputs: TaskInputs) -> TaskOutcome[dict[str, Any]]:
        """Stop at stability or maxSamples; incomplete runs cannot publish profiles."""
        value = inputs.upstream()
        calibration = self.settings.calibration
        with httpx.Client(trust_env=False) as http:
            for name, function in self.topology.function_settings.items():
                for key in self.topology.resources.nodes:
                    samples = []
                    for repetition in range(calibration.repetitions):
                        batch = []
                        stats = {"qualified": False}
                        for number in range(calibration.max_samples):
                            inputs.resource(self.clock).require_healthy()
                            sample = measure_invocation(
                                http,
                                value["runtimes"][f"{key}/{name}"],
                                payload=function.input,
                                execution_id="service-" + uuid4().hex,
                                expected_output=value["outputs"][name],
                                timeout_seconds=calibration.timeout_seconds,
                                release_timeout_seconds=calibration.timeout_seconds,
                            )
                            sample.update(
                                node=key,
                                function=name,
                                phase="service",
                                repetition=repetition,
                                number=number,
                            )
                            append_observation(self.run_dir / "samples.jsonl", sample)
                            batch.append(sample)
                            if len(batch) >= calibration.min_samples:
                                stats = summarize_service(
                                    batch,
                                    minimum_samples=calibration.min_samples,
                                    relative_ci=calibration.relative_ci,
                                    confidence=calibration.confidence,
                                    quantiles=calibration.quantiles,
                                    seed=self.settings.seed,
                                )
                                if stats["qualified"]:
                                    break
                        if not stats["qualified"]:
                            raise ValueError(
                                "NOT_QUALIFIED: service stability not reached"
                            )
                        samples.extend(batch)
                    value["samples"][f"{key}/{name}"] = samples
                    value["statistics"][f"{key}/{name}"] = summarize_service(
                        samples,
                        minimum_samples=calibration.min_samples,
                        relative_ci=calibration.relative_ci,
                        confidence=calibration.confidence,
                        quantiles=calibration.quantiles,
                        seed=self.settings.seed,
                    )
        return TaskOutcome(value=value)


class ValidateCapacityTask(_CalibrationTask[dict[str, Any]]):
    """Validate declared replica counts and co-location under measured paced work."""

    title = "Validate replica capacity and co-location range"

    def run(self, inputs: TaskInputs) -> TaskOutcome[dict[str, Any]]:
        """Measure each configuration; retain failed proof without widening validity."""
        value = inputs.upstream()
        calibration = self.settings.calibration
        if self.settings.purpose == "scientific-experiment":
            return TaskOutcome(value=self._validate_scientific(inputs, value))
        for key, node in self.topology.resources.nodes.items():
            endpoint = inputs.resource(self.topology.endpoints[key])
            for replicas in range(
                1,
                max(f.max_replicas for f in self.topology.function_settings.values())
                + 1,
            ):
                runtimes = []
                with httpx.Client(trust_env=False) as http:
                    for name, function in self.topology.function_settings.items():
                        if replicas > function.max_replicas:
                            continue
                        response = http.put(
                            endpoint + f"/v1/functions/{name}/replicas",
                            json={"replicas": replicas},
                            timeout=30,
                        )
                        response.raise_for_status()
                        for replica in range(1, replicas + 1):
                            runtime, _ = runtime_endpoint(
                                node, name, inputs=inputs, replica=replica
                            )
                            self._warm_replica(
                                http, runtime, name, function.input, value
                            )
                            runtimes.append((name, runtime, function))
                started = time.monotonic()
                duration = calibration.capacity_seconds

                def worker(
                    binding,
                    *,
                    key=key,
                    duration=duration,
                    started=started,
                    replicas=replicas,
                ):
                    name, runtime, function = binding
                    service = value["statistics"][f"{key}/{name}"]["meanSeconds"]
                    rate = function.utilization / service
                    count = max(2, int(rate * duration))
                    rows = []
                    with httpx.Client(trust_env=False) as http:
                        for number in range(count):
                            inputs.resource(self.clock).require_healthy()
                            delay = started + number / rate - time.monotonic()
                            if delay > 0:
                                time.sleep(delay)
                            sample = measure_invocation(
                                http,
                                runtime,
                                payload=function.input,
                                execution_id="capacity-" + uuid4().hex,
                                expected_output=value["outputs"][name],
                                timeout_seconds=calibration.timeout_seconds,
                                release_timeout_seconds=calibration.timeout_seconds,
                            )
                            sample.update(
                                node=key,
                                function=name,
                                phase="capacity",
                                replicas=replicas,
                            )
                            rows.append(sample)
                    return name, rows, time.monotonic() - started

                with ThreadPoolExecutor(max_workers=len(runtimes)) as pool:
                    results = list(pool.map(worker, runtimes))
                for name, function in self.topology.function_settings.items():
                    rows = [
                        row for f, batch, _ in results if f == name for row in batch
                    ]
                    if not rows:
                        continue
                    for row in rows:
                        append_observation(self.run_dir / "capacity-samples.jsonl", row)
                    stats = summarize_service(rows, minimum_samples=2, relative_ci=1)
                    service = value["statistics"][f"{key}/{name}"]["meanSeconds"]
                    model = (
                        min(replicas, function.max_replicas)
                        * function.utilization
                        / service
                    )
                    elapsed = max(duration, max(t for f, _, t in results if f == name))
                    measured = len(rows) / elapsed
                    relative_error = abs(measured / model - 1)
                    drift = abs(stats["meanSeconds"] / service - 1)
                    fact = {
                        "node": key,
                        "function": name,
                        "replicas": replicas,
                        "coLocation": sorted(self.topology.function_settings),
                        "plannedRate": model,
                        "completedRate": measured,
                        "completed": len(rows),
                        "elapsedSeconds": elapsed,
                        "relativeCapacityError": relative_error,
                        "relativeServiceDrift": drift,
                        "qualified": max(relative_error, drift)
                        <= calibration.capacity_error,
                    }
                    value["capacity"].append(fact)
                    append_observation(self.run_dir / "capacity.jsonl", fact)
                    if not fact["qualified"]:
                        raise ValueError(
                            "NOT_QUALIFIED: capacity/co-location model mismatch"
                        )
            with httpx.Client(trust_env=False) as http:
                for name in self.topology.function_settings:
                    http.put(
                        endpoint + f"/v1/functions/{name}/replicas",
                        json={"replicas": 1},
                    ).raise_for_status()
        return TaskOutcome(value=value)

    def _validate_scientific(self, inputs, value):
        if len(self.topology.function_settings) != 1:
            return self._validate_multi_scientific(inputs, value)
        name = next(iter(self.topology.function_settings))
        trials, capacities, maximum = [], {}, {}
        for node in self.settings.nodes:
            functions = resolve_node_functions(self.settings, node.id)
            key = next(iter(functions))
            maximum[node.id] = functions[key].max_replicas
            service = value["statistics"][f"{node.id}/{name}"]["meanSeconds"]
            for replicas in range(1, maximum[node.id] + 1):
                for factor in [0.5, 0.7, 0.9, 1.0, 1.1, 1.25]:
                    rate = factor * replicas / service
                    for repetition in range(3):
                        rows = run_capacity_trial(
                            self.topology,
                            self.settings,
                            inputs,
                            value,
                            replicas={node.id: replicas},
                            rates={node.id: rate},
                            repetition=repetition,
                            run_dir=self.run_dir
                            / "capacity-trials"
                            / f"{node.id}-r{replicas}-f{factor}-p{repetition}",
                            clock=self.clock,
                        )
                        trials.extend(rows)
                        for row in rows:
                            append_observation(self.run_dir / "capacity.jsonl", row)
                supported = select_capacity(
                    trials,
                    node=node.id,
                    replicas=replicas,
                    tolerance=self.settings.calibration.capacity_error,
                )
                modeled = replicas * functions[key].utilization / service
                if supported < modeled * (1 - self.settings.calibration.capacity_error):
                    raise ValueError("NOT_QUALIFIED: replica capacity model mismatch")
            capacities[node.id] = select_capacity(
                trials,
                node=node.id,
                replicas=maximum[node.id],
                tolerance=self.settings.calibration.capacity_error,
            )
        simultaneous = []
        for repetition in range(3):
            rows = run_capacity_trial(
                self.topology,
                self.settings,
                inputs,
                value,
                replicas=maximum,
                rates=capacities,
                repetition=repetition,
                run_dir=self.run_dir
                / "capacity-trials"
                / f"simultaneous-p{repetition}",
                clock=self.clock,
            )
            simultaneous.extend(rows)
            for row in rows:
                append_observation(
                    self.run_dir / "capacity.jsonl", {**row, "simultaneous": True}
                )
        for node in self.settings.nodes:
            select_capacity(
                simultaneous,
                node=node.id,
                replicas=maximum[node.id],
                tolerance=self.settings.calibration.capacity_error,
            )
        value["capacity"] = trials + simultaneous
        value["measuredCapacity"] = capacities
        return value

    def _validate_multi_scientific(self, inputs, value):
        if not self.settings.strict_memory:
            raise ValueError(
                "multi-function scientific capacity requires strict memory"
            )
        configurations = capacity_configurations(
            self.settings, self.topology.function_aliases
        )
        tolerance = self.settings.calibration.capacity_error
        local = {
            node.id: {
                self.topology.function_aliases[name]: fn
                for name, fn in resolve_node_functions(self.settings, node.id).items()
            }
            for node in self.settings.nodes
        }
        mixed_count = sum(entry["kind"] == "mixed" for entry in configurations)
        sweep_count = len(configurations) * len(CAPACITY_FACTORS) * 3
        write_immutable_artifact(
            self.run_dir / "capacity-plan.json",
            canonical_bytes(
                {
                    "schemaVersion": 2,
                    "configurations": configurations,
                    "factors": CAPACITY_FACTORS,
                    "repetitions": 3,
                    "warmupSeconds": 30,
                    "measurementSeconds": 60,
                    "sweepTrials": sweep_count,
                    "simultaneousTrials": 3 * mixed_count,
                    "minimumTrialWallSeconds": 90 * (sweep_count + 3 * mixed_count),
                    "additionalCosts": [
                        "provisioning",
                        "replica drain/start/warmup",
                        "evidence collection",
                        "cleanup",
                    ],
                    "statistics": value["statistics"],
                }
            ),
        )
        all_trials, records, signatures = [], [], set()

        def execute(vectors, rates, repetition, identifier):
            directory = self.run_dir / "capacity-trials" / identifier
            rows = run_capacity_trial(
                self.topology,
                self.settings,
                inputs,
                value,
                replicas=vectors,
                rates=rates,
                repetition=repetition,
                run_dir=directory,
                clock=self.clock,
            )
            all_trials.extend(rows)
            for row in rows:
                append_observation(self.run_dir / "capacity.jsonl", row)
            reference = {
                "path": str((directory / "trial.json").relative_to(self.run_dir)),
                "sha256": content_hash((directory / "trial.json").read_bytes()),
            }
            return rows, reference

        def record(node, vector, rates, references):
            signature = canonical_bytes([node, vector, rates])
            if signature not in signatures:
                signatures.add(signature)
                records.append(
                    {
                        "node": node,
                        "replicaVector": vector,
                        "rates": rates,
                        "trials": references,
                    }
                )

        for index, configuration in enumerate(configurations):
            vectors = configuration["vectors"]
            candidates = []
            for factor in CAPACITY_FACTORS:
                rates = {
                    node: {
                        name: round(
                            factor
                            * count
                            / value["statistics"][f"{node}/{name}"]["meanSeconds"]
                            * 90
                        )
                        / 90
                        for name, count in vector.items()
                    }
                    for node, vector in vectors.items()
                }
                if any(
                    count > 0 and rates[node][name] <= 0
                    for node, vector in vectors.items()
                    for name, count in vector.items()
                ):
                    raise ValueError(
                        "NOT_QUALIFIED: capacity arrival grid loses positive load"
                    )
                rows, references = [], []
                for repetition in range(3):
                    observed, reference = execute(
                        vectors, rates, repetition, f"c{index}-f{factor}-p{repetition}"
                    )
                    rows.extend(observed)
                    references.append(reference)
                candidates.append((rates, rows, references))
            selected = {}
            for node, vector in vectors.items():
                for rates, rows, references in reversed(candidates):
                    try:
                        select_mixed_capacity(
                            rows,
                            node=node,
                            vector=vector,
                            rates=rates[node],
                            tolerance=tolerance,
                        )
                    except ValueError:
                        continue
                    selected[node] = rates[node]
                    if any(
                        rates[node][name]
                        < count
                        * local[node][name].utilization
                        / value["statistics"][f"{node}/{name}"]["meanSeconds"]
                        * (1 - tolerance)
                        for name, count in vector.items()
                        if count
                    ):
                        raise ValueError(
                            "NOT_QUALIFIED: concurrent replica capacity model mismatch"
                        )
                    record(node, vector, rates[node], references)
                    break
                else:
                    raise ValueError(
                        f"NOT_QUALIFIED: no concurrent capacity for {node}"
                    )
            if configuration["kind"] == "mixed":
                rows, references = [], []
                for repetition in range(3):
                    observed, reference = execute(
                        vectors,
                        selected,
                        repetition,
                        f"simultaneous-c{index}-p{repetition}",
                    )
                    rows.extend(observed)
                    references.append(reference)
                for node, vector in vectors.items():
                    select_mixed_capacity(
                        rows,
                        node=node,
                        vector=vector,
                        rates=selected[node],
                        tolerance=tolerance,
                    )
                    # Prefer the simultaneous proof over the identical sweep record.
                    records[:] = [
                        entry
                        for entry in records
                        if not (
                            entry["node"] == node
                            and entry["replicaVector"] == vector
                            and entry["rates"] == selected[node]
                        )
                    ]
                    signatures.discard(canonical_bytes([node, vector, selected[node]]))
                    record(node, vector, selected[node], references)
        value["capacity"] = all_trials
        value["measuredConcurrentCapacity"] = records
        return value

    def _warm_replica(self, http, runtime, name, payload, value):
        deadline = time.monotonic() + 30
        while True:
            try:
                response = http.get(runtime + "/runtime/status", timeout=2)
                response.raise_for_status()
                status = response.json()
                if (
                    status.get("physicalReleaseProof") is not True
                    or status.get("maxConcurrentHandlers") != 1
                ):
                    raise ValueError("replica lacks physical proof")
                break
            except httpx.HTTPError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        sample = measure_invocation(
            http,
            runtime,
            payload=payload,
            execution_id="capacity-warmup-" + uuid4().hex,
            expected_output=value["outputs"][name],
            timeout_seconds=self.settings.calibration.timeout_seconds,
            release_timeout_seconds=self.settings.calibration.timeout_seconds,
        )
        append_observation(self.run_dir / "warmup.jsonl", sample)
        if sample["state"] != "complete":
            raise ValueError("capacity warmup failed")


class PublishCalibrationTask(
    _CalibrationTask[CalibrationProfile | CalibrationProfiles]
):
    """Publish a schema-valid measured artifact only after all validity checks."""

    title = "Publish immutable local calibration profile"

    def run(
        self, inputs: TaskInputs
    ) -> TaskOutcome[CalibrationProfile | CalibrationProfiles]:
        """Bind raw bytes, image IDs, input and the measured configuration scope."""
        value = inputs.upstream()
        built = inputs.resource(self.topology.distribution)
        if self.settings.purpose == "scientific-experiment":
            return TaskOutcome(value=self._publish_nodes(value, built))
        raw_hash = content_hash((self.run_dir / "samples.jsonl").read_bytes())
        functions = []
        for name, function in self.topology.function_settings.items():
            rows = [
                row
                for key, batch in value["samples"].items()
                if key.endswith("/" + name)
                for row in batch
            ]
            stats = summarize_service(
                rows,
                minimum_samples=self.settings.calibration.min_samples,
                relative_ci=self.settings.calibration.relative_ci,
                confidence=self.settings.calibration.confidence,
                quantiles=self.settings.calibration.quantiles,
                seed=self.settings.seed,
            )
            if not stats["qualified"] or any(
                abs(node_stats["meanSeconds"] / stats["meanSeconds"] - 1)
                > self.settings.calibration.capacity_error
                for key, node_stats in value["statistics"].items()
                if key.endswith("/" + name)
            ):
                raise ValueError("NOT_QUALIFIED: node service profiles differ")
            functions.append(
                {
                    "function": name,
                    "imageDigest": built.function(name, "rust").image.id,
                    "runtime": "HTTP",
                    "backend": "container-local",
                    "inputHash": "sha256:"
                    + content_hash(canonical_bytes(function.input)),
                    "cpuQuota": function.cpu,
                    "memoryMiB": function.memory_mib,
                    "replicas": 1,
                    "coLocation": sorted(
                        n for n in self.topology.function_settings if n != name
                    ),
                    "serviceSeconds": stats["meanSeconds"],
                    "statistics": {
                        k: stats[k]
                        for k in (
                            "sampleCount",
                            "meanSeconds",
                            "stddevSeconds",
                            "p95Seconds",
                        )
                    },
                    "validity": {
                        "minReplicas": 1,
                        "maxReplicas": function.max_replicas,
                        "maxRelativeCapacityError": (
                            self.settings.calibration.capacity_error
                        ),
                    },
                    "measurement": {
                        "warmupInvocations": (
                            self.settings.calibration.warmup_invocations
                        ),
                        "includesColdStarts": False,
                        "occupancy": "physical-handler",
                        "rawSamplesHash": "sha256:" + raw_hash,
                    },
                }
            )
        profile = CalibrationProfile.model_validate(
            {
                "schemaVersion": 1,
                "profileId": "local-" + raw_hash[:16],
                "provider": "multipass",
                "purpose": "workflow-validation",
                "synthetic": False,
                "sourceCommit": value["source"]["revision"],
                "environmentFingerprint": value["fingerprint"],
                "environment": value["environment"],
                "functions": functions,
            }
        )
        write_immutable_artifact(
            self.run_dir / "statistics.json", canonical_bytes(value["statistics"])
        )
        digest = write_immutable_artifact(
            self.run_dir / "profile.json",
            canonical_bytes(profile.model_dump(by_alias=True)),
        )
        write_immutable_artifact(
            self.run_dir / "profile-reference.json",
            canonical_bytes(
                {
                    "path": str(self.run_dir / "profile.json"),
                    "sha256": digest,
                }
            ),
        )
        return TaskOutcome(value=profile)

    def _publish_nodes(self, value, built) -> CalibrationProfiles:
        """Publish native v1 profiles without merging different physical demands."""
        profiles, references = {}, {}
        statistics = {}
        for node in self.settings.nodes:
            directory = self.run_dir / "nodes" / node.id
            functions = []
            local_samples = []
            for key, function in resolve_node_functions(self.settings, node.id).items():
                name = self.topology.function_aliases[key]
                rows = value["samples"][f"{node.id}/{name}"]
                stats = summarize_service(
                    rows,
                    minimum_samples=self.settings.calibration.min_samples,
                    relative_ci=self.settings.calibration.relative_ci,
                    confidence=self.settings.calibration.confidence,
                    quantiles=self.settings.calibration.quantiles,
                    seed=self.settings.seed,
                )
                if not stats["qualified"]:
                    raise ValueError("NOT_QUALIFIED: local service stability")
                statistics[f"{node.id}/{name}"] = stats
                local_samples.extend(rows)
                raw = b"".join(canonical_bytes(row) + b"\n" for row in rows)
                functions.append(
                    {
                        "function": name,
                        "imageDigest": built.function(name, "rust").image.id,
                        "runtime": "HTTP",
                        "backend": "container-local",
                        "inputHash": "sha256:"
                        + content_hash(canonical_bytes(function.input)),
                        "cpuQuota": function.cpu,
                        "memoryMiB": function.memory_mib,
                        "replicas": 1,
                        "coLocation": sorted(
                            other
                            for other in self.topology.function_settings
                            if other != name
                        ),
                        "serviceSeconds": stats["meanSeconds"],
                        "statistics": {
                            field: stats[field]
                            for field in (
                                "sampleCount",
                                "meanSeconds",
                                "stddevSeconds",
                                "p95Seconds",
                            )
                        },
                        "validity": {
                            "minReplicas": 1,
                            "maxReplicas": function.max_replicas,
                            "maxRelativeCapacityError": (
                                self.settings.calibration.capacity_error
                            ),
                        },
                        "measurement": {
                            "warmupInvocations": (
                                self.settings.calibration.warmup_invocations
                            ),
                            "includesColdStarts": False,
                            "occupancy": "physical-handler",
                            "rawSamplesHash": "sha256:" + content_hash(raw),
                        },
                    }
                )
            raw_samples = b"".join(
                canonical_bytes(row) + b"\n" for row in local_samples
            )
            write_immutable_artifact(directory / "samples.jsonl", raw_samples)
            profile = CalibrationProfile.model_validate(
                {
                    "schemaVersion": 1,
                    "profileId": node.id + "-" + content_hash(raw_samples)[:16],
                    "provider": self.settings.provider,
                    "purpose": self.settings.purpose,
                    "synthetic": False,
                    "sourceCommit": value["source"]["revision"],
                    "environmentFingerprint": value["nodeFingerprints"][node.id],
                    "environment": value["nodeEnvironments"][node.id],
                    "functions": functions,
                }
            )
            digest = write_immutable_artifact(
                directory / "profile.json",
                canonical_bytes(profile.model_dump(by_alias=True)),
            )
            profiles[node.id] = profile
            references[node.id] = {
                "path": str((directory / "profile.json").relative_to(self.run_dir)),
                "sha256": digest,
            }
        capacities, concurrent = None, None
        if len(self.topology.function_settings) > 1:
            from nanolab.one_shot.capacity import read_concurrent_capacity

            if not self.settings.strict_memory:
                raise ValueError("concurrent capacity requires strict memory")
            capacity_document = {
                "schemaVersion": 2,
                "artifactType": "measured-concurrent-capacity",
                "environmentFingerprint": value["fingerprint"],
                "functions": list(self.topology.function_settings),
                "capacities": value["measuredConcurrentCapacity"],
                "inferenceScope": (
                    "Exact measured vectors and rates; isolated capacities "
                    "do not qualify unmeasured mixes."
                ),
            }
            concurrent = read_concurrent_capacity(
                capacity_document,
                root=self.run_dir,
                nodes=set(profiles),
                functions=set(self.topology.function_settings),
                fingerprint=value["fingerprint"],
                tolerance=self.settings.calibration.capacity_error,
            )
        else:
            capacities = value["measuredCapacity"]
            if set(capacities) != set(profiles) or any(
                not math.isfinite(rate) or rate <= 0 for rate in capacities.values()
            ):
                raise ValueError("NOT_QUALIFIED: measured capacity scope incomplete")
            capacity_document = {
                "schemaVersion": 1,
                "artifactType": "measured-node-capacity",
                "environmentFingerprint": value["fingerprint"],
                "capacities": capacities,
                "trials": value.get("capacity", []),
            }
        capacity_hash = write_immutable_artifact(
            self.run_dir / "capacity-summary.json",
            canonical_bytes(capacity_document),
        )
        index = ProfileSet.model_validate(
            {
                "schemaVersion": 1,
                "artifactType": "node-profile-set",
                "provider": self.settings.provider,
                "purpose": self.settings.purpose,
                "environmentFingerprint": value["fingerprint"],
                "nodes": references,
                "nodeFingerprints": value["nodeFingerprints"],
                "capacity": {"path": "capacity-summary.json", "sha256": capacity_hash},
            }
        )
        write_immutable_artifact(
            self.run_dir / "statistics.json", canonical_bytes(statistics)
        )
        digest = write_immutable_artifact(
            self.run_dir / "profile.json",
            canonical_bytes(index.model_dump(mode="json", by_alias=True)),
        )
        write_immutable_artifact(
            self.run_dir / "profile-reference.json",
            canonical_bytes(
                {"path": str(self.run_dir / "profile.json"), "sha256": digest}
            ),
        )
        return CalibrationProfiles(
            profiles, digest, value["fingerprint"], capacities, concurrent
        )
