"""Frozen comparisons driven through APIs and an owned k6 generator resource."""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast
from uuid import uuid4

import httpx
from pydantic import JsonValue, TypeAdapter
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome

from nanolab.config.one_shot import ProtocolSettings, TimingCell, operational_limit
from nanolab.one_shot.campaign import read_campaign_binding
from nanolab.one_shot.client import NanoFaasOneShotClient
from nanolab.one_shot.models import (
    CalibrationProfile,
    CampaignManifest,
    TimingQualification,
)
from nanolab.one_shot.profiles import (
    CalibrationProfiles,
    profile_for_node,
    validate_trace_capacity,
)
from nanolab.one_shot.topology import observe_neighbors
from nanolab.one_shot.vm_resources import telemetry_policy
from nanolab.tasks.loadtest.soak import parse_prometheus
from nanolab.tasks.one_shot.artifacts import (
    append_observation,
    canonical_bytes,
    package_fingerprint,
    write_immutable_artifact,
)
from nanolab.tasks.one_shot.calibration import workload_checksum
from nanolab.tasks.one_shot.conservation import RunEvidence, evaluate_conservation
from nanolab.tasks.one_shot.preflight import runtime_endpoint, runtime_inventory
from nanolab.tasks.one_shot.qualification import (
    configuration_for_node,
    configure_node,
    prepare_parallel_epoch,
    verify_profile_scope,
    wait_until,
    wait_until_monotonic,
)
from nanolab.tasks.one_shot.trace import oracle_entries, request_schedule, warmup_trace
from nanolab.tasks.one_shot.verification import (
    realized_utility,
    validate_proof,
    verify_epoch_flows,
    verify_observations,
    verify_runtime_inventory,
    verify_warmup,
)

GENERATOR_DRAIN_SECONDS = 60


def read_qualification(settings) -> TimingQualification:
    """Reject changed, unqualified or incompatible independent timing evidence."""
    if settings.qualification is None:
        raise ValueError("independent timing qualification is required")
    result = TimingQualification.model_validate_json(
        settings.qualification.read_verified()
    )
    if not result.qualified:
        raise ValueError("timing artifact is NOT_QUALIFIED")
    if result.protocol_version != getattr(settings, "protocol_version", 1):
        raise ValueError("timing protocol version mismatch")
    if (result.provider, result.purpose, result.profile_sha256) != (
        settings.provider,
        settings.purpose,
        settings.profile.sha256,
    ):
        raise ValueError("timing provider/purpose/profile mismatch")
    if not result.matrix or not result.protocol:
        raise ValueError("timing qualification lacks protocol/matrix scope")
    expected_protocol = {
        field.alias or name for name, field in ProtocolSettings.model_fields.items()
    }
    if set(result.protocol) != expected_protocol:
        raise ValueError("timing qualification lacks complete protocol bounds")
    ProtocolSettings.model_validate(result.protocol)
    if (
        not result.period_candidates
        or result.period_seconds not in result.period_candidates
        or result.max_trace_resolution is None
        or result.period_seconds > result.max_trace_resolution
    ):
        raise ValueError("timing qualification lacks valid period/resolution scope")
    edges = {
        node.id
        for node in settings.nodes
        if settings.purpose == "scientific-experiment" or node.kind == "edge"
    }
    for cell in result.matrix:
        load = cell.get("load")
        if (
            not isinstance(load, dict)
            or load.get("method") != "owned-http-sdk-contention-v1"
            or load.get("valid") is not True
            or not load.get("physicalCompletions")
            or not load.get("emitted")
            or load.get("generatorDeficit") != 0
        ):
            raise ValueError(
                "timing qualification lacks accounted actual handler contention"
            )
        scope = TimingCell.model_validate(cell["cell"])
        if set(scope.rates) != edges or any(
            set(rates) != set(settings.functions) for rates in scope.rates.values()
        ):
            raise ValueError("timing matrix topology/function scope mismatch")
        observed = TimingQualification.model_validate(cell["result"])
        if (
            not observed.qualified
            or (
                observed.protocol_version,
                observed.provider,
                observed.purpose,
                observed.profile_sha256,
                observed.environment_fingerprint,
            )
            != (
                result.protocol_version,
                result.provider,
                result.purpose,
                result.profile_sha256,
                result.environment_fingerprint,
            )
            or observed.period_seconds > result.period_seconds
            or observed.lead_seconds > result.lead_seconds
        ):
            raise ValueError("timing matrix is not independently qualified")
    return result


def logical_schedule(manifest: CampaignManifest) -> tuple[str, float]:
    """Resolve the frozen host-local grid; a reboot cannot reuse monotonic times."""
    grid = manifest.parameters.get("logicalSchedule")
    if not isinstance(grid, dict) or grid.get("protocolVersion") != 2:
        raise ValueError("logical schedule missing from frozen manifest")
    session = grid.get("sessionId")
    anchor = grid.get("anchorMonotonicSeconds")
    if (
        not isinstance(session, str)
        or not session
        or not isinstance(anchor, (int, float))
        or isinstance(anchor, bool)
        or not math.isfinite(anchor)
        or anchor < 0
        or grid.get("hostBootId")
        != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    ):
        raise ValueError("invalid logical schedule or host boot identity mismatch")
    return session, float(anchor)


def verify_qualified_load(trace, qualification, *, aliases=None) -> None:
    """Require one actual qualified vector for every campaign vector, cloud included."""
    aliases = aliases or {name: name for name in trace.functions}
    vectors = [
        {
            node: {aliases.get(name, name): rate for name, rate in values.items()}
            for node, values in TimingCell.model_validate(cell["cell"]).rates.items()
        }
        for cell in qualification.matrix
    ]
    for window in trace.windows:
        if not any(
            set(vector) == set(window)
            and all(
                set(vector[node]) == set(window[node])
                and all(
                    abs(vector[node][name] - rate) <= 1e-9
                    for name, rate in window[node].items()
                )
                for node in window
            )
            for vector in vectors
        ):
            raise ValueError("experiment vector lacks independent timing qualification")


METRICS = (
    "nanofaas_offload_total",
    "nanofaas_offload_failure_total",
    "nanofaas_runtime_active_handlers",
    "nanofaas_runtime_replica_occupancy_seconds_count",
    "nanofaas_runtime_replica_occupancy_seconds_sum",
    "execution_in_flight_records",
    "function_queue_rejected_total",
    "function_enqueue_total",
    "executor_queued_tasks",
    "sync_queue_depth",
    "sync_queue_admitted_total",
    "sync_queue_rejected_total",
    "nanofaas_oneshot_auction_seconds_sum",
    "nanofaas_oneshot_auction_seconds_count",
    "nanofaas_oneshot_solver_seconds_sum",
    "nanofaas_oneshot_messages_total",
    "nanofaas_oneshot_rounds_total",
    "process_cpu_usage",
    "jvm_memory_used_bytes",
)


def verify_ready_plan(plan: dict, *, memory_capacity: int) -> None:
    """Check each published ready allocation independently of client throughput."""
    used = 0
    for row in plan["functions"].values():
        replicas = row["readyReplicas"]
        used += replicas * row["memoryMiB"]
        inbound = sum(
            assignment["quantity"] * plan["flowQuantum"]
            for assignment in row["inbound"]
        )
        capacity = replicas * row["utilization"] / row["demandSeconds"]
        if row["localRate"] + inbound > capacity + 1e-9:
            raise ValueError("ready plan exceeds physical modeled capacity")
        if any(not assignment["readyConfirmed"] for assignment in row["inbound"]):
            raise ValueError("inbound assignment lacks ready confirmation")
    if used > memory_capacity:
        raise ValueError("ready plan exceeds RAM budget")


class FreezeManifestTask(Task[CampaignManifest]):
    """Verify measured scope and persist immutable manifest before generator acquire."""

    title = "Freeze verified one-shot comparison manifest"

    def __init__(
        self,
        topology,
        settings,
        profile: CalibrationProfile | CalibrationProfiles,
        qualification,
        trace,
        *,
        mode,
        repetition,
        modes,
        clock,
        run_dir,
    ):
        """Bind declared prerequisites and per-run identities."""
        self.topology, self.settings, self.profile, self.qualification, self.trace = (
            topology,
            settings,
            profile,
            qualification,
            trace,
        )
        self.mode, self.repetition, self.modes, self.clock, self.run_dir = (
            mode,
            repetition,
            modes,
            clock,
            run_dir,
        )

    def run(self, inputs: TaskInputs) -> TaskOutcome[CampaignManifest]:
        """Match fingerprints and cloud capacity before freezing original times."""
        built = inputs.resource(self.topology.distribution)
        fingerprint = verify_profile_scope(
            self.profile, self.settings, self.topology, inputs.upstream(), built
        )
        if self.qualification.environment_fingerprint != fingerprint:
            raise ValueError("timing environment fingerprint mismatch")
        inputs.resource(self.clock).require_healthy()
        experiment = self.settings.experiment
        cloud = next(node for node in self.settings.nodes if node.kind == "cloud")
        node_functions = getattr(self.topology, "node_function_settings", {})
        concurrent = (
            isinstance(self.profile, CalibrationProfiles)
            and self.profile.concurrent_capacities is not None
        )
        if concurrent:
            from nanolab.one_shot.capacity import comparison_replica_vectors

            static = comparison_replica_vectors(
                self.settings, self.topology.function_aliases
            )
        else:
            static = {
                node.id: {
                    name: fn.max_replicas
                    for name, fn in node_functions.get(
                        node.id, self.topology.function_settings
                    ).items()
                }
                for node in self.settings.nodes
            }
        for node in self.settings.nodes:
            functions = node_functions.get(node.id, self.topology.function_settings)
            memory = sum(
                static[node.id][name] * fn.memory_mib for name, fn in functions.items()
            )
            if memory > node.memory_capacity_mib:
                raise ValueError("static comparison replicas exceed node RAM budget")
        cloud_profile = cast(Any, profile_for_node(self.profile, cloud.id))
        measured = {row["function"]: row for row in cloud_profile.functions}
        cloud_functions = node_functions.get(cloud.id, self.topology.function_settings)
        if isinstance(self.profile, CalibrationProfiles):
            validate_trace_capacity(self.trace, self.profile, self.settings)
        for window in [] if concurrent else self.trace.windows:
            terminal_occupancy = sum(
                sum(rates[name] for rates in window.values())
                * measured[name]["serviceSeconds"]
                / cloud_functions[name].max_replicas
                for name in self.trace.functions
            )
            if terminal_occupancy > 0.9:
                raise ValueError(
                    "terminal cloud lacks declared ten percent physical headroom"
                )
        warmup_seconds = (
            max(experiment.warmup_seconds, 2 * self.trace.period_seconds)
            if self.settings.purpose == "scientific-experiment"
            else experiment.warmup_seconds
        )
        if warmup_seconds > (720 if concurrent else 120):
            raise ValueError("scientific warmup exceeds configured maximum")
        captured_at = datetime.now(UTC)
        captured_mono = time.monotonic()
        delay = warmup_seconds + (60 if concurrent else 20)
        anchor = captured_at + timedelta(seconds=delay)
        run_id = f"{self.trace.sha256[:12]}:{self.mode}:{self.repetition}"
        binding = read_campaign_binding(self.settings)
        if binding is not None:
            if (
                self.modes != binding["block"]["modes"]
                or self.trace.sha256 != binding["trace"].sha256
            ):
                raise ValueError("execution differs from frozen campaign order/trace")
            run_id += f":{binding['sha256'][:12]}:{binding['block']['block']}"
        parameters = {
            "nodes": [node.model_dump(by_alias=True) for node in self.settings.nodes],
            "functions": {
                key: fn.model_dump(by_alias=True)
                for key, fn in self.topology.function_settings.items()
            },
            "images": {
                component.name: component.image.id for component in built.components
            },
            "protocol": self.qualification.protocol,
            "timing": self.qualification.model_dump(),
            "generator": {
                **experiment.model_dump(by_alias=True),
                "warmupSeconds": warmup_seconds,
                "version": subprocess.run(
                    ("k6", "version"),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).stdout.strip(),
                "retry": False,
                "location": "operator-host",
            },
            "nanolabVersion": version("nanolab"),
            "nanolabPackageSha256": package_fingerprint(),
            **(
                {"vmTelemetry": telemetry_policy()}
                if self.settings.strict_memory and len(self.settings.functions) > 1
                else {}
            ),
            "network": "observed-local; shared physical host",
            "cloudMemoryCapacityMiB": cloud.memory_capacity_mib,
            "terminalCloudQueueing": True,
            "serviceDriftToleranceByFunction": {
                row["function"]: min(
                    self.settings.calibration.capacity_error,
                    row["validity"]["maxRelativeCapacityError"],
                )
                for row in cloud_profile.functions
            },
            "admissionProfile": "SYNC_QUEUE",
            "metricsProfile": "advanced",
            "baselinePolicy": {
                "mode": "pressure",
                "edgeSyncQueue": True,
                "edgeMaxDepth": max(
                    fn.max_replicas for fn in self.topology.function_settings.values()
                ),
                "replicas": {
                    name: fn.max_replicas
                    for name, fn in self.topology.function_settings.items()
                },
            },
            "source": built.source,
            "nodeEndpoints": {
                key: inputs.resource(endpoint)
                for key, endpoint in self.topology.endpoints.items()
            },
        }
        if self.settings.protocol_version == 2:
            if self.qualification.protocol_version != 2:
                raise ValueError("timing protocol version mismatch")
            parameters["logicalSchedule"] = {
                "protocolVersion": 2,
                "sessionId": uuid4().hex,
                "anchorMonotonicSeconds": captured_mono + delay,
                "capturedMonotonicSeconds": captured_mono,
                "capturedAtUtc": captured_at.isoformat(),
                "hostBootId": Path("/proc/sys/kernel/random/boot_id")
                .read_text()
                .strip(),
            }
        if isinstance(self.profile, CalibrationProfiles):
            parameters["nodeFunctions"] = {
                node: {
                    name: fn.model_dump(by_alias=True) for name, fn in functions.items()
                }
                for node, functions in node_functions.items()
            }
            parameters["nodeProfiles"] = {
                node: local.model_dump(by_alias=True)
                for node, local in self.profile.by_node.items()
            }
            parameters["capacities"] = self.profile.capacities
            if concurrent:
                parameters["concurrentCapacities"] = self.profile.concurrent_capacities
                parameters["staticReplicaVectors"] = static
            parameters["peerGraph"] = self.topology.neighbor_graph
            parameters["phaseIds"] = self.trace.phase_ids
            parameters["nodeRoles"] = self.trace.node_roles
            parameters["requestedWindows"] = self.trace.requested_windows
            parameters.pop("serviceDriftToleranceByFunction")
            parameters["serviceDriftToleranceByNodeFunction"] = {
                node: {
                    row["function"]: min(
                        self.settings.calibration.capacity_error,
                        row["validity"]["maxRelativeCapacityError"],
                    )
                    for row in cast(list[dict[str, Any]], local.functions)
                }
                for node, local in self.profile.by_node.items()
            }
            parameters["baselinePolicy"] = {
                "mode": "pressure",
                "edgeSyncQueue": True,
                "edgeMaxDepth": {
                    node: max(fn.max_replicas for fn in functions.values())
                    for node, functions in node_functions.items()
                },
                "replicas": {
                    node: static[node] for node, functions in node_functions.items()
                },
            }
        if binding is not None:
            parameters["campaign"] = {
                "block": binding["block"]["block"],
                "seed": binding["block"]["seed"],
                "role": binding["protocol"]["role"],
                "plannedBlocks": binding["protocol"]["plannedBlocks"],
                "protocolSha256": binding["sha256"],
            }
        manifest = CampaignManifest(
            provider=self.settings.provider,
            purpose=self.settings.purpose,
            environment_fingerprint=fingerprint,
            source_commit=cloud_profile.source_commit,
            profile_sha256=self.settings.profile.sha256,
            qualification_sha256=self.settings.qualification.sha256,
            trace_sha256=self.trace.sha256,
            seed=self.settings.seed,
            nodes=[node.id for node in self.settings.nodes],
            functions=self.trace.functions,
            period_seconds=self.trace.period_seconds,
            lead_seconds=self.qualification.lead_seconds,
            flow_quantum=self.trace.flow_quantum,
            modes=self.modes,
            run_id=run_id,
            mode=self.mode,
            repetition=self.repetition,
            anchor=anchor.isoformat(),
            parameters=parameters,
        )
        endpoints = {
            key: inputs.resource(endpoint)
            for key, endpoint in self.topology.endpoints.items()
        }
        schedule = request_schedule(
            self.trace, anchor=anchor, endpoints=endpoints, run_id=run_id
        )
        from nanolab.one_shot.profiles import read_profiles

        capacities = (
            read_profiles(self.settings).capacities
            if self.settings.purpose == "scientific-experiment"
            else None
        )
        warmup = warmup_trace(
            self.trace,
            seconds=warmup_seconds,
            capacities=capacities,
            legacy_rate=experiment.warmup_rate,
        )
        warm = request_schedule(
            warmup,
            anchor=anchor - timedelta(seconds=warmup_seconds),
            endpoints=endpoints,
            run_id=run_id + ":warmup",
        )
        warm = [{**row, "phase": "warmup", "epoch": -1} for row in warm]
        for path, content in (
            ("manifest.json", manifest.model_dump()),
            ("trace.json", self.trace.model_dump()),
            (
                "schedule.json",
                sorted(
                    warm + schedule,
                    key=lambda row: (row["scheduledAt"], row["originalId"]),
                ),
            ),
        ):
            write_immutable_artifact(self.run_dir / path, canonical_bytes(content))
        return TaskOutcome(value=manifest)


class UploadForecastTask(Task[None]):
    """Configure baseline or forecast source without changing node decisions."""

    title = "Configure comparison mode and confirm forecast upload"

    def __init__(self, freeze: FreezeManifestTask):
        """Reuse the frozen run context and its immutable manifest."""
        self.freeze = freeze

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Use public APIs; baseline leaves the one-shot router unconfigured."""
        run = self.freeze
        manifest = CampaignManifest.model_validate_json(
            (run.run_dir / "manifest.json").read_bytes()
        )
        protocol = ProtocolSettings.model_validate(run.qualification.protocol)
        cloud_key = next(node.id for node in run.settings.nodes if node.kind == "cloud")
        cloud_url = inputs.resource(run.topology.endpoints[cloud_key])
        from nanolab.one_shot.capacity import comparison_replica_vectors

        aliases = getattr(
            run.topology,
            "function_aliases",
            {key: key.removesuffix("-rust") for key in run.settings.functions},
        )
        static = (
            comparison_replica_vectors(run.settings, aliases)
            if run.settings.strict_memory and len(run.settings.functions) > 1
            else {
                node: {
                    name: fn.max_replicas
                    for name, fn in getattr(run.topology, "node_function_settings", {})
                    .get(node, run.topology.function_settings)
                    .items()
                }
                for node in run.topology.endpoints
            }
        )
        with httpx.Client(trust_env=False) as http:
            for key, endpoint in run.topology.endpoints.items():
                url = inputs.resource(endpoint)
                node = run.topology.resources.nodes[key]
                if (
                    node.config.kind == "cloud"
                    or run.mode == "baseline"
                    or (run.settings.strict_memory and len(run.settings.functions) > 1)
                ):
                    functions = getattr(run.topology, "node_function_settings", {}).get(
                        key, run.topology.function_settings
                    )
                    for name in functions:
                        http.put(
                            url + f"/v1/functions/{name}/replicas",
                            json={"replicas": static[key][name]},
                            timeout=30,
                        ).raise_for_status()
                        for replica in range(1, static[key][name] + 1):
                            runtime_endpoint(node, name, inputs=inputs, replica=replica)
                if node.config.kind == "cloud":
                    continue
                if run.mode == "baseline":
                    continue
                client = NanoFaasOneShotClient(url, http=http)
                client.load_profile(profile_for_node(run.profile, key), revision=0)
                conf = configuration_for_node(
                    run.topology,
                    run.settings,
                    run.profile,
                    key,
                    client=client,
                    cloud_uri=cloud_url,
                    period=manifest.period_seconds,
                    lead=manifest.lead_seconds,
                    epsilon=run.qualification.epsilon,
                    protocol=protocol,
                    anchor=datetime.fromisoformat(manifest.anchor),
                    session_id=(
                        logical_schedule(manifest)[0]
                        if run.settings.protocol_version == 2
                        else None
                    ),
                )
                configure_node(
                    client,
                    conf,
                    revision=client.status().revision,
                    output=run.run_dir / "api.jsonl",
                )
                if run.mode == "oracle":
                    receipt = client.load_trace(
                        {
                            "schemaVersion": 1,
                            "nodeId": key,
                            "revision": 1,
                            "provider": "oracle",
                            "producedAt": datetime.now(UTC).isoformat(),
                            "entries": cast(
                                JsonValue,
                                oracle_entries(
                                    run.trace,
                                    node=key,
                                    generations=client.status().catalog_generations,
                                    anchor=datetime.fromisoformat(manifest.anchor),
                                ),
                            ),
                        },
                        revision=0,
                    )
                    append_observation(
                        run.run_dir / "api.jsonl",
                        {
                            "action": "trace-upload",
                            "node": key,
                            "receipt": receipt.model_dump(mode="json", by_alias=True),
                        },
                    )
        return TaskOutcome()


def generator_resource(
    *,
    run_dir: Path,
    vus: int,
    duration: float,
    telemetry: Resource | None = None,
    settings=None,
) -> Resource[subprocess.Popen]:
    """Sonata owns the only k6 child, including termination after task failures."""
    # k6 resolves open() relative to its JS module, not the operator's cwd.
    run_dir = run_dir.resolve()
    streams = []

    def acquire(_inputs):
        try:
            output = (run_dir / "generator.jsonl").open("w")
            streams.append(output)
            errors = (run_dir / "generator.stderr").open("w")
            streams.append(errors)
            remaining = duration
            if getattr(settings, "operational_limits", None) is not None:
                manifest = json.loads((run_dir / "manifest.json").read_text())
                grid = manifest["parameters"]["logicalSchedule"]
                remaining = max(
                    0,
                    grid["anchorMonotonicSeconds"]
                    + grid["epochCount"] * manifest["period_seconds"]
                    - time.monotonic(),
                )
            drain = operational_limit(
                settings, "generator_drain_seconds", GENERATOR_DRAIN_SECONDS
            )
            environment = {
                **os.environ,
                "ONE_SHOT_SCHEDULE": str(run_dir / "schedule.json"),
                "ONE_SHOT_VUS": str(vus),
                "ONE_SHOT_DURATION": f"{math.ceil(remaining + drain)}s",
            }
            return subprocess.Popen(
                (
                    "k6",
                    "run",
                    "--quiet",
                    "--log-format=json",
                    "--log-output=stdout",
                    str(files("nanolab").joinpath("assets/k6/one-shot.js")),
                ),
                stdout=output,
                stderr=errors,
                env=environment,
            )
        except BaseException:
            for stream in streams:
                stream.close()
            raise

    def release(_inputs, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(
                    timeout=operational_limit(settings, "process_exit_seconds", 5)
                )
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(
                    timeout=operational_limit(settings, "process_exit_seconds", 5)
                )
        for stream in streams:
            stream.close()

    return Resource(
        title="Own bounded one-shot k6 generator",
        acquire=acquire,
        release=release,
        requires=(telemetry,) if telemetry is not None else (),
    )


def _observation_rows(path: Path) -> list[dict]:
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def generator_rows(path: Path) -> list[dict]:
    """Retain valid original event messages without mistaking k6 summaries for data."""
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        try:
            envelope = json.loads(line)
            row = json.loads(envelope["msg"])
        except (ValueError, KeyError, TypeError):
            continue
        if row.get("event") in {"emitted", "result"}:
            rows.append(row)
    return rows


def physical_attempts(path: Path) -> list[dict]:
    """Deduplicate repeated reads of a proof while preserving distinct attempts."""
    observations = (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )
    unique = {}
    for row in observations:
        key = (
            row["originalId"],
            row.get("executionId"),
            row.get("incarnation"),
            row.get("dispatchAttempt"),
            row.get("destination"),
        )
        unique[key] = row
    return list(unique.values())


class LiveEvidenceCollector:
    """Capture proofs during load so an epoch transition cannot erase all history."""

    def __init__(self, run, inputs):
        """Store owned stop state and the current Sonata resource inputs."""
        self.run, self.inputs = run, inputs
        self.stop = Event()
        self.error = None
        self.event_cursors = {}
        self.pending: dict[str, dict] = {}
        self.thread = Thread(target=self.loop, name="one-shot-evidence", daemon=False)
        self.thread.start()

    def loop(self):
        """Retry missing proofs while retaining observations and metric samples."""
        while not self.stop.wait(0.5):
            try:
                seen = {
                    row["originalId"]
                    for row in physical_attempts(self.run.run_dir / "physical.jsonl")
                    if row.get("state") == "RELEASED"
                }
                for identity in tuple(self.pending):
                    if identity in seen:
                        del self.pending[identity]
                for row in generator_rows(self.run.run_dir / "generator.jsonl"):
                    if (
                        row["phase"] == "campaign"
                        and row["event"] == "result"
                        and row["originalId"] not in seen
                    ):
                        self.pending.setdefault(row["originalId"], row)
                progress = collect_runtime_evidence(
                    self.run,
                    self.inputs,
                    list(self.pending.values()),
                    stop=self.stop,
                    deadline=time.monotonic()
                    + operational_limit(
                        getattr(self.run, "settings", None), "process_drain_seconds", 30
                    ),
                )
                visited, released = progress or ([], set())
                seen.update(released)
                # Resume past this pass's visited prefix, including failed reads.
                # Rebuilding from generator order would starve newer proofs.
                for identity in visited:
                    row = self.pending.pop(identity)
                    if identity not in seen:
                        self.pending[identity] = row
                append_observation(
                    self.run.run_dir / "evidence-collection.jsonl",
                    {
                        "atUtc": datetime.now(UTC).isoformat(),
                        "visitedOriginals": len(visited),
                        "pendingOriginals": len(self.pending),
                        "provenOriginals": len(seen),
                    },
                )
                self.collect_events()
            except Exception as error:
                self.error = error
                return

    def collect_events(self):
        """Export runtime events incrementally without enlarging native retention."""
        settings = getattr(self.run, "settings", None)
        if getattr(settings, "operational_limits", None) is None:
            return
        manifest = CampaignManifest.model_validate_json(
            (self.run.run_dir / "manifest.json").read_bytes()
        )
        if manifest.parameters.get("observationOnly") is not True:
            return
        _session, anchor = logical_schedule(manifest)
        epoch = math.floor(
            (time.monotonic() + manifest.lead_seconds - anchor)
            / manifest.period_seconds
        )
        if epoch < 0:
            return
        epoch = min(epoch, len(self.run.trace.windows) - 1)
        with httpx.Client(trust_env=False) as http:
            for node in self.run.topology.neighbor_graph:
                if self.stop.is_set():
                    return
                client = NanoFaasOneShotClient(
                    self.inputs.resource(self.run.topology.endpoints[node]),
                    http=http,
                    timeout_seconds=operational_limit(
                        settings, "collection_http_seconds", 10
                    ),
                )
                for current in range(max(0, epoch - 1), epoch + 1):
                    after = self.event_cursors.get((node, current), 0)
                    try:
                        pages = client.iter_epoch_events(
                            current,
                            after=after,
                            max_pages=int(
                                operational_limit(settings, "event_pages", 100)
                            ),
                        )
                        for page in pages:
                            if self.stop.is_set():
                                return
                            if not page.entries:
                                continue
                            append_observation(
                                self.run.run_dir / "runtime-events.jsonl",
                                {
                                    "atUtc": datetime.now(UTC).isoformat(),
                                    "node": node,
                                    "epoch": current,
                                    "after": after,
                                    "page": page.model_dump(mode="json", by_alias=True),
                                    "retentionCoverage": (
                                        "not-provable-from-cursor-alone"
                                    ),
                                },
                            )
                            after = page.entries[-1].cursor
                            self.event_cursors[node, current] = after
                    except (httpx.HTTPError, ValueError, RuntimeError) as error:
                        append_observation(
                            self.run.run_dir / "runtime-events.jsonl",
                            {
                                "atUtc": datetime.now(UTC).isoformat(),
                                "node": node,
                                "epoch": current,
                                "after": after,
                                "collectionError": str(error),
                                "measurementComplete": False,
                            },
                        )
                        continue

    def require_healthy(self):
        """Surface failures of the independent evidence sampler."""
        if self.error is not None:
            raise RuntimeError(
                "live physical evidence collector failed"
            ) from self.error

    def close(self):
        """Join the owned collector before releasing any runtime resource."""
        self.stop.set()
        self.thread.join(
            timeout=operational_limit(
                getattr(getattr(self, "run", None), "settings", None),
                "worker_join_seconds",
                40,
            )
        )
        if self.thread.is_alive():
            raise RuntimeError("live evidence collector cleanup unconfirmed")


def evidence_collector_resource(run, generator) -> Resource[LiveEvidenceCollector]:
    """Keep evidence collection alive exactly through the owned load task."""
    return Resource(
        title="Own live physical evidence collector",
        acquire=lambda inputs: LiveEvidenceCollector(run, inputs),
        release=lambda _inputs, collector: collector.close(),
        requires=(*run.topology.requires, generator),
    )


def collect_runtime_evidence(
    run,
    inputs,
    rows: list[dict],
    *,
    stop: Event | None = None,
    deadline: float | None = None,
) -> tuple[list[str], set[str]]:
    """Preserve evidence and return visited originals and released proof IDs."""
    visited: list[str] = []
    released: set[str] = set()
    runtimes = []
    sampled_at = datetime.now(UTC).isoformat()

    def cancelled():
        return (stop is not None and stop.is_set()) or (
            deadline is not None and time.monotonic() >= deadline
        )

    def read_timeout():
        configured = operational_limit(
            getattr(run, "settings", None), "collection_http_seconds", 2
        )
        return (
            configured
            if deadline is None
            else min(configured, max(0.001, deadline - time.monotonic()))
        )

    if cancelled():
        return visited, released
    with httpx.Client(trust_env=False) as http:
        if getattr(run.topology, "neighbor_graph", None):
            snapshot = observe_neighbors(
                http,
                {
                    key: inputs.resource(run.topology.endpoints[key])
                    for key in run.topology.neighbor_graph
                },
                run.topology.neighbor_graph,
            )
            append_observation(
                run.run_dir / "peer-graph.jsonl", {"at": sampled_at, "nodes": snapshot}
            )
        for node_id, node in run.topology.resources.nodes.items():
            if cancelled():
                break
            functions = getattr(run.topology, "node_function_settings", {}).get(
                node_id, run.topology.function_settings
            )
            settings = getattr(run, "settings", None)
            candidates = []
            if (
                settings is not None
                and settings.strict_memory
                and len(settings.functions) > 1
            ):
                try:
                    candidates = runtime_inventory(
                        node, functions=list(functions), inputs=inputs
                    )
                except Exception as error:
                    append_observation(
                        run.run_dir / "collection-errors.jsonl",
                        {"node": node_id, "error": str(error)},
                    )
            else:
                fixed = getattr(run, "runtime_replica_counts", {}).get(node_id)
                for name, function in functions.items():
                    count = (
                        function.max_replicas if fixed is None else fixed.get(name, 0)
                    )
                    for replica in range(1, count + 1):
                        if cancelled():
                            break
                        try:
                            url, container = runtime_endpoint(
                                node, name, inputs=inputs, replica=replica
                            )
                            candidates.append((name, replica, url, container))
                        except Exception as error:
                            append_observation(
                                run.run_dir / "collection-errors.jsonl",
                                {
                                    "node": node_id,
                                    "function": name,
                                    "replica": replica,
                                    "error": str(error),
                                },
                            )
            for name, replica, url, container in candidates:
                if cancelled():
                    break
                try:
                    status = http.get(
                        url + "/runtime/status",
                        timeout=read_timeout(),
                    )
                    status.raise_for_status()
                    runtimes.append((node_id, name, url))
                    append_observation(
                        run.run_dir / "runtime-inventory.jsonl",
                        {
                            "at": sampled_at,
                            "node": node_id,
                            "function": name,
                            "replica": replica,
                            "url": url,
                            "container": container,
                            "status": status.json(),
                        },
                    )
                    if cancelled():
                        break
                    metrics = http.get(
                        url + "/metrics",
                        timeout=read_timeout(),
                    )
                    metrics.raise_for_status()
                    append_observation(
                        run.run_dir / "metrics.jsonl",
                        {
                            "at": datetime.now(UTC).isoformat(),
                            "node": node_id,
                            "function": name,
                            "replica": replica,
                            "metrics": parse_prometheus(metrics.text, METRICS),
                        },
                    )
                    (
                        run.run_dir / f"{node_id}-{name}-{replica}-metrics.txt"
                    ).write_text(metrics.text)
                except (httpx.HTTPError, ValueError, RuntimeError) as error:
                    append_observation(
                        run.run_dir / "collection-errors.jsonl",
                        {
                            "node": node_id,
                            "function": name,
                            "replica": replica,
                            "error": str(error),
                        },
                    )

        def lookup(row):
            proofs = []
            if cancelled():
                return False, proofs
            if not row.get("terminalExecutionId", row.get("executionId")):
                return True, proofs
            targets = getattr(run, "sdk_request_targets", {})
            target = targets.get(row["originalId"])
            candidates = runtimes
            if target is not None:
                node_id, name, target_url = target
                if (
                    row.get("sdk") is not True
                    or row.get("url") != target_url
                    or row["function"] != name
                ):
                    append_observation(
                        run.run_dir / "collection-errors.jsonl",
                        {
                            "originalId": row["originalId"],
                            "error": "direct SDK target differs from frozen schedule",
                        },
                    )
                    return True, proofs
                candidates = [
                    runtime
                    for runtime in runtimes
                    if runtime == (node_id, name, target_url.removesuffix("/invoke"))
                ]
            attempted = not any(name == row["function"] for _, name, _ in candidates)
            for node_id, name, url in candidates:
                if cancelled():
                    break
                if name != row["function"]:
                    continue
                attempted = True
                read_attempt = 1
                try:
                    proof_url = (
                        url
                        + "/runtime/executions/"
                        + row.get("terminalExecutionId", row.get("executionId"))
                    )
                    try:
                        response = http.get(
                            proof_url,
                            timeout=read_timeout(),
                        )
                    except (httpx.TransportError, OSError) as error:
                        append_observation(
                            run.run_dir / "collection-errors.jsonl",
                            {
                                "originalId": row["originalId"],
                                "node": node_id,
                                "readAttempt": read_attempt,
                                "error": str(error),
                            },
                        )
                        if cancelled():
                            return True, proofs
                        read_attempt = 2
                        try:
                            response = http.get(proof_url, timeout=read_timeout())
                        except OSError as error:
                            raise httpx.ReadError(str(error)) from error
                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                    if not validate_proof(
                        response.json(),
                        row.get("terminalExecutionId", row.get("executionId")),
                    ):
                        raise ValueError("SDK proof identity/attempt mismatch")
                    proofs.append(
                        {
                            **row,
                            **response.json(),
                            "destination": node_id,
                            "runtimeUrl": url,
                        }
                    )
                except (httpx.HTTPError, ValueError, RuntimeError) as error:
                    append_observation(
                        run.run_dir / "collection-errors.jsonl",
                        {
                            "originalId": row["originalId"],
                            "node": node_id,
                            "readAttempt": read_attempt,
                            "error": str(error),
                        },
                    )
            return attempted, proofs

        with ThreadPoolExecutor(max_workers=16) as executor:
            for offset in range(0, len(rows), 16):
                if cancelled():
                    break
                batch = [
                    row
                    for row in rows[offset : offset + 16]
                    if row["event"] == "result" and row["phase"] == "campaign"
                ]
                for row, (attempted, proofs) in zip(
                    batch, executor.map(lookup, batch), strict=True
                ):
                    if attempted:
                        visited.append(row["originalId"])
                    for proof in proofs:
                        append_observation(run.run_dir / "physical.jsonl", proof)
                        if proof.get("state") == "RELEASED":
                            released.add(proof["originalId"])
        for node_id, endpoint in run.topology.endpoints.items():
            if cancelled():
                break
            try:
                url = inputs.resource(endpoint)
                response = http.get(
                    url.replace(":8080", ":9090") + "/actuator/prometheus",
                    timeout=read_timeout(),
                )
                response.raise_for_status()
                append_observation(
                    run.run_dir / "metrics.jsonl",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "node": node_id,
                        "metrics": parse_prometheus(response.text, METRICS),
                    },
                )
                (run.run_dir / f"{node_id}-metrics.txt").write_text(response.text)
            except (httpx.HTTPError, ValueError, RuntimeError) as error:
                append_observation(
                    run.run_dir / "collection-errors.jsonl",
                    {"node": node_id, "error": str(error)},
                )
    if cancelled():
        append_observation(
            run.run_dir / "collection-errors.jsonl",
            {
                "atUtc": datetime.now(UTC).isoformat(),
                "measurementComplete": False,
                "reason": "collection stopped or snapshot deadline reached",
            },
        )

    return visited, released


class RunOneShotLoadTask(Task[None]):
    """Trigger each epoch once while the independent absolute k6 schedule runs."""

    title = "Run original load and collect best-effort physical evidence"

    def __init__(
        self,
        freeze,
        generator,
        collector,
        telemetry=None,
        *,
        enforce_timing_budget=True,
    ):
        """Bind the owned generator and already frozen topology."""
        self.freeze, self.generator, self.collector = freeze, generator, collector
        self.telemetry = telemetry
        self.enforce_timing_budget = enforce_timing_budget

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Never retry an ambiguous prepare; collect evidence even on failure."""
        run = self.freeze
        manifest = CampaignManifest.model_validate_json(
            (run.run_dir / "manifest.json").read_bytes()
        )
        anchor = datetime.fromisoformat(manifest.anchor)
        process, clock = inputs.resource(self.generator), inputs.resource(run.clock)
        observation_only = manifest.parameters.get("observationOnly") is True
        collector = inputs.resource(self.collector)
        telemetry = (
            inputs.resource(self.telemetry) if self.telemetry is not None else None
        )
        failure = None
        primary_error = None
        try:
            if run.settings.experiment.fail_load:
                raise RuntimeError("injected load-task failure")
            logical = getattr(run.settings, "protocol_version", 1) == 2
            session_id, mono_anchor = (
                logical_schedule(manifest) if logical else (None, 0.0)
            )
            with httpx.Client(trust_env=False) as http:
                clients = {
                    key: NanoFaasOneShotClient(
                        inputs.resource(run.topology.endpoints[key]),
                        http=http,
                        timeout_seconds=operational_limit(
                            run.settings, "api_seconds", 30
                        ),
                    )
                    for key in run.trace.nodes
                    if not getattr(run.trace, "node_roles", None)
                    or run.trace.node_roles[key] == "edge"
                }
                for epoch in range(len(run.trace.windows)):
                    start = anchor + timedelta(seconds=epoch * run.trace.period_seconds)
                    activation_at = mono_anchor + epoch * manifest.period_seconds
                    if logical:
                        wait_until_monotonic(
                            activation_at - manifest.lead_seconds, clock=clock
                        )
                    else:
                        wait_until(
                            start - timedelta(seconds=manifest.lead_seconds),
                            clock=clock,
                        )
                    collector.require_healthy()
                    if telemetry is not None:
                        telemetry.require_healthy()
                    if process.poll() is not None and not (
                        getattr(run, "supervision", None) is not None
                        and process.returncode == 0
                        and run.generator_complete()
                    ):
                        raise RuntimeError("generator stopped before scheduled epochs")
                    if run.mode != "baseline":
                        sample, _observation = prepare_parallel_epoch(
                            clients,
                            epoch=epoch,
                            starts_at=start,
                            ends_at=start + timedelta(seconds=manifest.period_seconds),
                            output=run.run_dir / "epochs.jsonl",
                            **(
                                {"observation_only": True, "settings": run.settings}
                                if observation_only
                                else {}
                            ),
                            **(
                                {
                                    "session_id": session_id,
                                    "activation_at": activation_at,
                                    "clock": clock,
                                }
                                if logical
                                else {}
                            ),
                        )
                        for node_id, node in _observation["nodes"].items():
                            result = node.get("result")
                            if result and result.get("plan"):
                                verify_ready_plan(
                                    result["plan"],
                                    memory_capacity=run.topology.resources.nodes[
                                        node_id
                                    ].config.memory_capacity_mib,
                                )
                        plans = {
                            node: observation["result"]["plan"]
                            for node, observation in _observation["nodes"].items()
                            if (observation.get("result") or {}).get("plan")
                        }
                        if not observation_only and len(plans) != len(clients):
                            raise ValueError("missing native ready allocation")
                        missing_nodes = set(clients) - set(plans)
                        flow_observations = verify_epoch_flows(
                            plans,
                            neighbor_graph=getattr(run.topology, "neighbor_graph", None)
                            or None,
                            **(
                                {
                                    "observation_only": True,
                                    "unavailable_nodes": missing_nodes,
                                }
                                if observation_only
                                else {}
                            ),
                        )
                        if missing_nodes:
                            flow_observations.append(
                                {
                                    "kind": "incompletePlanSet",
                                    "missingNodes": sorted(missing_nodes),
                                }
                            )
                        if flow_observations:
                            append_observation(
                                run.run_dir / "assignment-observations.jsonl",
                                {"epoch": epoch, "observations": flow_observations},
                            )
                        if observation_only:
                            append_observation(
                                run.run_dir / "epoch-observations.jsonl",
                                {
                                    "atUtc": datetime.now(UTC).isoformat(),
                                    "epoch": epoch,
                                    "censored": sample.censored,
                                    "missingPlanNodes": sorted(
                                        set(clients) - set(plans)
                                    ),
                                    "readySeconds": sample.ready_seconds,
                                    "auctionSeconds": sample.auction_seconds,
                                    "readinessLagSeconds": _observation.get(
                                        "readinessLagSeconds"
                                    ),
                                    "negotiationWallSeconds": _observation.get(
                                        "negotiationWallSeconds"
                                    ),
                                    "activationWallSeconds": _observation.get(
                                        "activationWallSeconds"
                                    ),
                                },
                            )
                        if not observation_only and (
                            sample.censored
                            or sample.ready_seconds is None
                            or sample.auction_seconds is None
                            or (
                                self.enforce_timing_budget
                                and not observation_only
                                and (
                                    sample.ready_seconds > manifest.lead_seconds
                                    or sample.auction_seconds
                                    + run.qualification.margin_seconds
                                    > run.qualification.epsilon
                                    * manifest.period_seconds
                                )
                            )
                        ):
                            raise RuntimeError(
                                "epoch exceeds its qualified wall/readiness budget"
                            )
            if logical and getattr(run, "supervision", None) is not None:
                # A completed shared-iterations generator can finish before the
                # boundary. Keep collectors alive through the complete last slot.
                wait_until_monotonic(
                    mono_anchor + len(run.trace.windows) * manifest.period_seconds,
                    clock=clock,
                )
            limit = anchor + timedelta(
                seconds=len(run.trace.windows) * manifest.period_seconds
                + operational_limit(run.settings, "generator_drain_seconds", 35)
            )
            mono_limit = (
                mono_anchor
                + len(run.trace.windows) * manifest.period_seconds
                + operational_limit(run.settings, "generator_drain_seconds", 35)
            )
            while process.poll() is None:
                clock.require_healthy()
                collector.require_healthy()
                if telemetry is not None:
                    telemetry.require_healthy()
                if (
                    time.monotonic() > mono_limit
                    if logical
                    else datetime.now(UTC) > limit
                ):
                    raise TimeoutError("load generator exceeded its bounded duration")
                if logical:
                    wait_until_monotonic(time.monotonic() + 0.1, clock=clock)
                else:
                    wait_until(datetime.now(UTC) + timedelta(seconds=0.1), clock=clock)
            if process.returncode:
                raise RuntimeError(f"load generator failed: exit {process.returncode}")
        except BaseException as error:
            failure = str(error)
            primary_error = error
            raise
        finally:
            supervision = getattr(run, "supervision", None)
            if supervision is not None:
                for name in ("generator", "collector", "sampler"):
                    supervision.unwatch(name)
            secondary = []

            def attempt(action):
                try:
                    action()
                except Exception as error:
                    secondary.append(error)

            def stop_child():
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(
                            timeout=operational_limit(
                                run.settings, "process_exit_seconds", 5
                            )
                        )
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(
                            timeout=operational_limit(
                                run.settings, "process_exit_seconds", 5
                            )
                        )

            attempt(collector.close)
            attempt(stop_child)
            if telemetry is not None:
                attempt(telemetry.close)
            attempt(
                lambda: append_observation(
                    run.run_dir / "load-status.jsonl",
                    {"failure": failure, "exitCode": process.returncode},
                )
            )
            if primary_error is None and not secondary:

                def final_snapshot():
                    proven = {
                        row["originalId"]
                        for row in physical_attempts(run.run_dir / "physical.jsonl")
                        if row.get("state") == "RELEASED"
                    }
                    collect_runtime_evidence(
                        run,
                        inputs,
                        [
                            row
                            for row in generator_rows(run.run_dir / "generator.jsonl")
                            if row["originalId"] not in proven
                        ],
                        deadline=time.monotonic()
                        + operational_limit(run.settings, "process_drain_seconds", 30),
                    )

                attempt(final_snapshot)
            if primary_error is not None:
                attempt(lambda: collect_original_evidence(run, inputs))
            for error in secondary[:]:
                attempt(
                    lambda error=error: append_observation(
                        run.run_dir / "collection-errors.jsonl", {"error": str(error)}
                    )
                )
            if primary_error is not None:
                for error in secondary:
                    primary_error.add_note(
                        "Secondary cleanup/collection failure: " + str(error)
                    )
            elif secondary:
                for error in secondary[1:]:
                    secondary[0].add_note(
                        "Secondary cleanup/collection failure: " + str(error)
                    )
                raise secondary[0]
        return TaskOutcome()


def classify_result(row: dict, *, expected: str, physical: list[dict]) -> dict:
    """Distinguish proven terminal outcomes from ambiguous remote failures."""
    success = (
        row["statusCode"] == 200
        and str(row["responseStatus"]).upper() == "SUCCESS"
        and row.get("outputJson") == expected
    )
    from nanolab.one_shot.returned_errors import classify_response

    explicit_refusal = classify_response(row) == "reject"
    terminal_error = not success and (
        (400 <= row["statusCode"] < 500 and row["statusCode"] != 408)
        or explicit_refusal
        or (
            str(row["responseStatus"]).upper() == "ERROR"
            and any(proof.get("state") == "RELEASED" for proof in physical)
        )
    )
    return {**row, "success": success, "terminalError": terminal_error}


class CollectEvidenceTask(Task[RunEvidence]):
    """Build counts from original events and independent SDK release proof."""

    title = "Correlate original IDs and physical completions"

    def __init__(self, freeze):
        """Read evidence only from the current frozen run directory."""
        self.freeze = freeze

    def run(self, inputs: TaskInputs) -> TaskOutcome[RunEvidence]:
        """Keep incomplete and censored rows visible in the evidence."""
        return TaskOutcome(value=collect_original_evidence(self.freeze, inputs))


def collect_original_evidence(run, inputs: TaskInputs) -> RunEvidence:
    """Correlate raw originals independently of workflow task success."""
    planned = [
        row
        for row in json.loads((run.run_dir / "schedule.json").read_bytes())
        if row["phase"] == "campaign"
    ]
    rows = [
        row
        for row in generator_rows(run.run_dir / "generator.jsonl")
        if row["phase"] == "campaign"
    ]
    emitted = [row for row in rows if row["event"] == "emitted"]
    expected = {
        name: str(workload_checksum(function.input, function=name))
        for name, function in run.topology.function_settings.items()
    }
    physical = run.run_dir / "physical.jsonl"
    attempts = physical_attempts(physical)
    results = [
        classify_result(
            row,
            expected=expected[row["function"]],
            physical=[
                proof for proof in attempts if proof["originalId"] == row["originalId"]
            ],
        )
        for row in rows
        if row["event"] == "result"
    ]
    evidence = evaluate_conservation(planned, emitted, attempts, results)
    warmup = verify_warmup(
        json.loads((run.run_dir / "schedule.json").read_bytes()),
        generator_rows(run.run_dir / "generator.jsonl"),
        max_lateness=run.settings.experiment.max_arrival_lateness,
    )
    if not warmup["valid"]:
        evidence.valid = False
        evidence.violations.append(
            "warmup arrivals/outcomes do not match frozen schedule"
        )
    for status in _observation_rows(run.run_dir / "load-status.jsonl"):
        if status.get("failure") or status.get("exitCode") not in (None, 0):
            evidence.valid = False
            evidence.violations.append("load task failed: " + str(status))
    lateness = [row["emittedAt"] - row["scheduledAt"] for row in emitted]
    if lateness and max(lateness) > run.settings.experiment.max_arrival_lateness:
        evidence.valid = False
        evidence.violations.append("generator arrivals exceed declared lateness")
    violations, services = verify_observations(
        attempts,
        results,
        profile=run.profile,
        tolerance=run.settings.calibration.capacity_error,
        require_execution_node=run.mode != "baseline",
        cloud=next(node.id for node in run.settings.nodes if node.kind == "cloud"),
        nodes=list(run.topology.resources.nodes),
        endpoints={
            key: inputs.resource(endpoint)
            for key, endpoint in run.topology.endpoints.items()
        },
    )
    inventory_path = run.run_dir / "runtime-inventory.jsonl"
    inventory = (
        [json.loads(line) for line in inventory_path.read_text().splitlines()]
        if inventory_path.exists()
        else []
    )
    violations += verify_runtime_inventory(
        inventory,
        functions=run.topology.function_settings,
        node_functions=getattr(run.topology, "node_function_settings", None),
        nodes=run.topology.resources.nodes,
    )
    if violations:
        evidence.valid = False
        evidence.violations.extend(violations)
    resource_path = run.run_dir / "vm-resources" / "coverage.json"
    resource_coverage = (
        json.loads(resource_path.read_text()) if resource_path.exists() else None
    )
    if (
        run.settings.strict_memory
        and len(run.settings.functions) > 1
        and (not resource_coverage or not resource_coverage["complete"])
    ):
        evidence.valid = False
        evidence.violations.append("whole-VM resource telemetry coverage incomplete")
    evidence.observations = TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "realizedUtility": realized_utility(
                evidence.cells,
                functions=run.topology.function_settings,
                cloud=next(
                    node.id for node in run.settings.nodes if node.kind == "cloud"
                ),
            )
            if evidence.valid
            else None,
            "utilityConvention": (
                "(alpha*local + delta*peer - gamma*(cloud+errors))/planned "
                "originals; final solver zero-price convention, "
                "excludes bidding transfers"
            ),
            "mode": run.mode,
            "repetition": run.repetition,
            "latenciesSeconds": [row["latencySeconds"] for row in results],
            "arrivalLatenessSeconds": lateness,
            "physicalServiceByNodeFunction": services,
            "censored": evidence.censored,
            "loadExecuted": bool(emitted),
            "plannedWindows": run.trace.windows,
            "physicalAttempts": attempts,
            "warmup": warmup,
            "results": results,
            "epochs": _observation_rows(run.run_dir / "epochs.jsonl"),
            "metricSamples": _observation_rows(run.run_dir / "metrics.jsonl"),
            "vmResources": resource_coverage,
        }
    )
    write_immutable_artifact(
        run.run_dir / "evidence.json", canonical_bytes(evidence.model_dump())
    )
    from nanolab.one_shot.report import render_report

    manifest = CampaignManifest.model_validate_json(
        (run.run_dir / "manifest.json").read_bytes()
    )
    render_report(manifest, evidence, run.run_dir / "report.html")
    return evidence


class EvaluateOneShotTask(Task[RunEvidence]):
    """Reject nonconforming comparisons while retaining measured results."""

    title = "Verify one-shot comparison assumptions and conservation"

    def run(self, inputs: TaskInputs) -> TaskOutcome[RunEvidence]:
        """Algorithm performance is not a pass criterion; evidence completeness is."""
        evidence: RunEvidence = inputs.upstream()
        if not evidence.valid:
            raise ValueError(
                "nonconforming comparison: " + "; ".join(evidence.violations)
            )
        return TaskOutcome(value=evidence)
