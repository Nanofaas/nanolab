"""Frozen comparisons driven through APIs and an owned k6 generator resource."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from threading import Event, Thread
from typing import cast

import httpx
from pydantic import JsonValue, TypeAdapter
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome

from nanolab.config.one_shot import ProtocolSettings, TimingCell
from nanolab.one_shot.client import NanoFaasOneShotClient
from nanolab.one_shot.models import CampaignManifest, TimingQualification
from nanolab.tasks.loadtest.soak import parse_prometheus
from nanolab.tasks.one_shot.artifacts import (
    append_observation,
    canonical_bytes,
    content_hash,
    write_immutable_artifact,
)
from nanolab.tasks.one_shot.calibration import workload_checksum
from nanolab.tasks.one_shot.conservation import RunEvidence, evaluate_conservation
from nanolab.tasks.one_shot.preflight import runtime_endpoint
from nanolab.tasks.one_shot.qualification import (
    configuration_for_node,
    configure_node,
    prepare_parallel_epoch,
    verify_profile_scope,
    wait_until,
)
from nanolab.tasks.one_shot.trace import oracle_entries, request_schedule
from nanolab.tasks.one_shot.verification import (
    realized_utility,
    validate_proof,
    verify_epoch_flows,
    verify_observations,
    verify_runtime_inventory,
)


def package_fingerprint() -> str:
    """Bind the installed implementation/assets, including a dirty local checkout."""
    root = Path(str(files("nanolab")))
    entries = {
        str(path.relative_to(root)): content_hash(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.suffix in {".py", ".js", ".json", ".yaml", ".yml", ".example"}
    }
    return content_hash(canonical_bytes(entries))


def read_qualification(settings) -> TimingQualification:
    """Reject changed, unqualified or incompatible independent timing evidence."""
    if settings.qualification is None:
        raise ValueError("independent timing qualification is required")
    result = TimingQualification.model_validate_json(
        settings.qualification.read_verified()
    )
    if not result.qualified:
        raise ValueError("timing artifact is NOT_QUALIFIED")
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
    edges = {node.id for node in settings.nodes if node.kind == "edge"}
    for cell in result.matrix:
        scope = TimingCell.model_validate(cell["cell"])
        if set(scope.rates) != edges or any(
            set(rates) != set(settings.functions) for rates in scope.rates.values()
        ):
            raise ValueError("timing matrix topology/function scope mismatch")
        observed = TimingQualification.model_validate(cell["result"])
        if (
            not observed.qualified
            or (
                observed.provider,
                observed.purpose,
                observed.profile_sha256,
                observed.environment_fingerprint,
            )
            != (
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
        profile,
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
        for node in self.settings.nodes:
            memory = sum(
                fn.max_replicas * fn.memory_mib
                for fn in self.topology.function_settings.values()
            )
            cpu = sum(
                fn.max_replicas * fn.cpu
                for fn in self.topology.function_settings.values()
            )
            if memory > node.memory_capacity_mib or cpu > node.cpus:
                raise ValueError(
                    "static comparison replicas exceed node RAM/CPU budget"
                )
        measured = {row["function"]: row for row in self.profile.functions}
        for window in self.trace.windows:
            terminal_occupancy = sum(
                sum(rates[name] for rates in window.values())
                * measured[name]["serviceSeconds"]
                / self.topology.function_settings[name].max_replicas
                for name in self.trace.functions
            )
            if terminal_occupancy > 0.9:
                raise ValueError(
                    "terminal cloud lacks declared ten percent physical headroom"
                )
        anchor = datetime.now(UTC) + timedelta(seconds=25)
        run_id = f"{self.trace.sha256[:12]}:{self.mode}:{self.repetition}"
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
                "version": subprocess.run(
                    ("k6", "version"), check=True, capture_output=True, text=True
                ).stdout.strip(),
                "retry": False,
                "location": "operator-host",
            },
            "nanolabVersion": version("nanolab"),
            "nanolabPackageSha256": package_fingerprint(),
            "network": "observed-local; shared physical host",
            "cloudMemoryCapacityMiB": cloud.memory_capacity_mib,
            "terminalCloudQueueing": True,
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
        manifest = CampaignManifest(
            provider=self.settings.provider,
            purpose=self.settings.purpose,
            environment_fingerprint=fingerprint,
            source_commit=self.profile.source_commit,
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
        warmup = self.trace.model_copy(
            update={
                "windows": [
                    {
                        node: dict.fromkeys(
                            self.trace.functions, experiment.warmup_rate
                        )
                        for node in self.trace.nodes
                    }
                ],
                "period_seconds": float(experiment.warmup_seconds),
            }
        )
        warmup = type(warmup).model_validate(warmup.model_dump())
        warm = request_schedule(
            warmup,
            anchor=anchor - timedelta(seconds=experiment.warmup_seconds),
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
        with httpx.Client(trust_env=False) as http:
            for key, endpoint in run.topology.endpoints.items():
                url = inputs.resource(endpoint)
                node = run.topology.resources.nodes[key]
                if node.config.kind == "cloud" or run.mode == "baseline":
                    for name, function in run.topology.function_settings.items():
                        http.put(
                            url + f"/v1/functions/{name}/replicas",
                            json={"replicas": function.max_replicas},
                            timeout=30,
                        ).raise_for_status()
                        for replica in range(1, function.max_replicas + 1):
                            runtime_endpoint(node, name, inputs=inputs, replica=replica)
                if node.config.kind == "cloud":
                    continue
                if run.mode == "baseline":
                    continue
                client = NanoFaasOneShotClient(url, http=http)
                client.load_profile(run.profile, revision=0)
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
    *, run_dir: Path, vus: int, duration: float
) -> Resource[subprocess.Popen]:
    """Sonata owns the only k6 child, including termination after task failures."""
    streams = []

    def acquire(_inputs):
        output = (run_dir / "generator.jsonl").open("w")
        errors = (run_dir / "generator.stderr").open("w")
        streams.extend((output, errors))
        environment = {
            **os.environ,
            "ONE_SHOT_SCHEDULE": str(run_dir / "schedule.json"),
            "ONE_SHOT_VUS": str(vus),
            "ONE_SHOT_DURATION": f"{int(duration + 60)}s",
        }
        try:
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
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in streams:
            stream.close()

    return Resource(
        title="Own bounded one-shot k6 generator", acquire=acquire, release=release
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
                rows = [
                    row
                    for row in generator_rows(self.run.run_dir / "generator.jsonl")
                    if row["phase"] == "campaign"
                    and row["event"] == "result"
                    and row["originalId"] not in seen
                ]
                collect_runtime_evidence(self.run, self.inputs, rows)
            except Exception as error:
                self.error = error
                return

    def require_healthy(self):
        """Surface failures of the independent evidence sampler."""
        if self.error is not None:
            raise RuntimeError(
                "live physical evidence collector failed"
            ) from self.error

    def close(self):
        """Join the owned collector before releasing any runtime resource."""
        self.stop.set()
        self.thread.join(timeout=40)
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


def collect_runtime_evidence(run, inputs, rows: list[dict]) -> None:
    """Preserve independent physical proof, metrics and cleanup diagnostics."""
    runtimes = []
    sampled_at = datetime.now(UTC).isoformat()
    with httpx.Client(trust_env=False) as http:
        for node_id, node in run.topology.resources.nodes.items():
            for name, function in run.topology.function_settings.items():
                for replica in range(1, function.max_replicas + 1):
                    try:
                        url, container = runtime_endpoint(
                            node, name, inputs=inputs, replica=replica
                        )
                        status = http.get(url + "/runtime/status", timeout=2)
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
                        metrics = http.get(url + "/metrics", timeout=2)
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

        def lookup(row):
            proofs = []
            if not row.get("terminalExecutionId", row.get("executionId")):
                return proofs
            for node_id, name, url in runtimes:
                if name != row["function"]:
                    continue
                try:
                    response = http.get(
                        url
                        + "/runtime/executions/"
                        + row.get("terminalExecutionId", row.get("executionId")),
                        timeout=2,
                    )
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
                except Exception as error:
                    append_observation(
                        run.run_dir / "collection-errors.jsonl",
                        {
                            "originalId": row["originalId"],
                            "node": node_id,
                            "error": str(error),
                        },
                    )
            return proofs

        with ThreadPoolExecutor(max_workers=16) as executor:
            for proofs in executor.map(
                lookup,
                [
                    row
                    for row in rows
                    if row["event"] == "result" and row["phase"] == "campaign"
                ],
            ):
                for proof in proofs:
                    append_observation(run.run_dir / "physical.jsonl", proof)
        for node_id, endpoint in run.topology.endpoints.items():
            try:
                url = inputs.resource(endpoint)
                response = http.get(
                    url.replace(":8080", ":9090") + "/actuator/prometheus", timeout=2
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
            except Exception as error:
                append_observation(
                    run.run_dir / "collection-errors.jsonl",
                    {"node": node_id, "error": str(error)},
                )


class RunOneShotLoadTask(Task[None]):
    """Trigger each epoch once while the independent absolute k6 schedule runs."""

    title = "Run original load and collect best-effort physical evidence"

    def __init__(self, freeze, generator, collector):
        """Bind the owned generator and already frozen topology."""
        self.freeze, self.generator, self.collector = freeze, generator, collector

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Never retry an ambiguous prepare; collect evidence even on failure."""
        run = self.freeze
        manifest = CampaignManifest.model_validate_json(
            (run.run_dir / "manifest.json").read_bytes()
        )
        anchor = datetime.fromisoformat(manifest.anchor)
        process, clock = inputs.resource(self.generator), inputs.resource(run.clock)
        collector = inputs.resource(self.collector)
        failure = None
        try:
            if run.settings.experiment.fail_load:
                raise RuntimeError("injected load-task failure")
            with httpx.Client(trust_env=False) as http:
                clients = {
                    key: NanoFaasOneShotClient(
                        inputs.resource(run.topology.endpoints[key]), http=http
                    )
                    for key in run.trace.nodes
                }
                for epoch in range(len(run.trace.windows)):
                    start = anchor + timedelta(seconds=epoch * run.trace.period_seconds)
                    wait_until(
                        start - timedelta(seconds=manifest.lead_seconds), clock=clock
                    )
                    collector.require_healthy()
                    if process.poll() is not None:
                        raise RuntimeError("generator stopped before scheduled epochs")
                    if run.mode != "baseline":
                        sample, _observation = prepare_parallel_epoch(
                            clients,
                            epoch=epoch,
                            starts_at=start,
                            ends_at=start + timedelta(seconds=manifest.period_seconds),
                            output=run.run_dir / "epochs.jsonl",
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
                            if observation.get("result", {}).get("plan")
                        }
                        if len(plans) != len(clients):
                            raise ValueError("missing native ready allocation")
                        verify_epoch_flows(plans)
                        if (
                            sample.censored
                            or sample.ready_seconds is None
                            or sample.ready_seconds > manifest.lead_seconds
                            or sample.auction_seconds is None
                            or sample.auction_seconds + run.qualification.margin_seconds
                            > run.qualification.epsilon * manifest.period_seconds
                        ):
                            raise RuntimeError(
                                "epoch exceeds its qualified wall/readiness budget"
                            )
            limit = anchor + timedelta(
                seconds=len(run.trace.windows) * manifest.period_seconds + 35
            )
            while process.poll() is None:
                clock.require_healthy()
                collector.require_healthy()
                if datetime.now(UTC) > limit:
                    raise TimeoutError("load generator exceeded its bounded duration")
                wait_until(datetime.now(UTC) + timedelta(seconds=0.1), clock=clock)
            if process.returncode:
                raise RuntimeError(f"load generator failed: exit {process.returncode}")
        except BaseException as error:
            failure = str(error)
            raise
        finally:
            cleanup_error = None
            try:
                collector.close()
            except Exception as error:
                cleanup_error = error
                append_observation(
                    run.run_dir / "collection-errors.jsonl", {"error": str(error)}
                )
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            append_observation(
                run.run_dir / "load-status.jsonl",
                {"failure": failure, "exitCode": process.returncode},
            )
            try:
                collect_runtime_evidence(
                    run, inputs, generator_rows(run.run_dir / "generator.jsonl")
                )
            except Exception as error:
                append_observation(
                    run.run_dir / "collection-errors.jsonl", {"error": str(error)}
                )
            if failure is not None:
                try:
                    collect_original_evidence(run, inputs)
                except Exception as error:
                    append_observation(
                        run.run_dir / "collection-errors.jsonl", {"error": str(error)}
                    )
            if cleanup_error is not None and failure is None:
                raise cleanup_error
        return TaskOutcome()


def classify_result(row: dict, *, expected: str, physical: list[dict]) -> dict:
    """Distinguish proven terminal outcomes from ambiguous remote failures."""
    success = (
        row["statusCode"] == 200
        and str(row["responseStatus"]).upper() == "SUCCESS"
        and row.get("outputJson") == expected
    )
    try:
        body = json.loads(row.get("responseBody") or "{}")
    except (ValueError, TypeError):
        body = {}
    explicit_refusal = (
        row["statusCode"] == 502
        and isinstance(body, dict)
        and body.get("error") == "OFFLOAD_FAILED"
        and "returned 429" in str(body.get("message", ""))
    )
    proxy_refusal = (
        isinstance(body, dict)
        and body.get("error")
        == {"code": "EXTERNAL_ERROR", "message": "Too many concurrent invocations"}
        and row["statusCode"] == 200
        and str(row["responseStatus"]).upper() == "ERROR"
    )
    terminal_error = not success and (
        proxy_refusal
        or (400 <= row["statusCode"] < 500 and row["statusCode"] != 408)
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
        name: str(workload_checksum(function.input))
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
        nodes=run.topology.resources.nodes,
    )
    if violations:
        evidence.valid = False
        evidence.violations.extend(violations)
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
            "results": results,
            "epochs": _observation_rows(run.run_dir / "epochs.jsonl"),
            "metricSamples": _observation_rows(run.run_dir / "metrics.jsonl"),
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
