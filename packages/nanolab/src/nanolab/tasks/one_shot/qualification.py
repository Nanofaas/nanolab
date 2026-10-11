"""Measured manual preparation through NanoFaaS APIs; no auction decisions here."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock
from typing import Any
from uuid import uuid4

import httpx
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome

from nanolab.config.one_shot import (
    OneShotConfig,
    ProtocolSettings,
    resolve_node_functions,
)
from nanolab.one_shot.client import NanoFaasOneShotClient
from nanolab.one_shot.models import (
    CalibrationProfile,
    TimingQualification,
    verify_calibration,
)
from nanolab.one_shot.profiles import (
    CalibrationProfiles,
    profile_for_node,
    read_profiles,
)
from nanolab.tasks.loadtest.soak import parse_prometheus
from nanolab.tasks.one_shot.artifacts import (
    append_observation,
    canonical_bytes,
    content_hash,
    write_immutable_artifact,
)
from nanolab.tasks.one_shot.calibration import (
    environment_identity,
    node_environment_identity,
)
from nanolab.tasks.one_shot.clock import ClockMonitor
from nanolab.tasks.one_shot.preflight import runtime_endpoint
from nanolab.tasks.one_shot.qualification_load import QualificationLoad
from nanolab.tasks.one_shot.timing import WallTimingSample, select_timing

AUCTION_SUM = "nanofaas_oneshot_auction_seconds_sum"
AUCTION_COUNT = "nanofaas_oneshot_auction_seconds_count"
SOLVER_SUM = "nanofaas_oneshot_solver_seconds_sum"


def capture_replica_diagnostics(topology, inputs, output: Path, *, epoch: int) -> None:
    """Retain scoped container state after a censored transition without retrying it."""
    for node_id, node in topology.resources.nodes.items():
        for name, function in topology.function_settings.items():
            for replica in range(1, function.max_replicas + 1):
                row: dict[str, Any] = {
                    "epoch": epoch,
                    "node": node_id,
                    "function": name,
                    "replica": replica,
                }
                try:
                    url, container = runtime_endpoint(
                        node, name, inputs=inputs, replica=replica
                    )
                    row.update(url=url, container=container)
                except Exception as error:
                    row["error"] = str(error)
                append_observation(output, row)


def duration(seconds: float) -> str:
    """Encode seconds for the Java Duration JSON contract."""
    return f"PT{seconds:.9f}S"


def read_profile(settings: OneShotConfig) -> CalibrationProfile | CalibrationProfiles:
    """Reject altered or synthetic prerequisite bytes before any VM acquisition."""
    if settings.purpose == "scientific-experiment":
        return read_profiles(settings)
    effective = [resolve_node_functions(settings, node.id) for node in settings.nodes]
    if any(functions != effective[0] for functions in effective[1:]):
        raise ValueError("single profile requires homogeneous resource limits")
    if settings.profile is None:
        raise ValueError("independent measured profile is required")
    profile = CalibrationProfile.model_validate(
        json.loads(settings.profile.read_verified())
    )
    verify_calibration(
        profile,
        provider=settings.provider,
        purpose=settings.purpose,
        fingerprint=profile.environment_fingerprint,
    )
    return profile


def verify_profile_scope(profile, settings, topology, preflight, built):
    """Match current measured identity and every runtime/input validity field."""
    fingerprint, _ = environment_identity(settings, preflight, built)
    if isinstance(profile, CalibrationProfiles):
        if profile.environment_fingerprint != fingerprint or set(profile.by_node) != {
            n.id for n in settings.nodes
        }:
            raise ValueError("profile-set environment/node scope mismatch")
        for node in settings.nodes:
            local_fingerprint, _ = node_environment_identity(
                settings, preflight, built, node.id
            )
            local = profile.by_node[node.id]
            functions = topology.node_function_settings[node.id]
            _verify_native_scope(
                local,
                settings,
                built,
                functions,
                local_fingerprint,
                {name: fn.max_replicas for name, fn in functions.items()},
            )
    else:
        _verify_native_scope(
            profile,
            settings,
            built,
            topology.function_settings,
            fingerprint,
            {
                name: max(
                    node.memory_capacity_mib // fn.memory_mib for node in settings.nodes
                )
                for name, fn in topology.function_settings.items()
            },
        )
    return fingerprint


def _verify_native_scope(profile, settings, built, functions, fingerprint, maxima):
    """Check one measured native profile against only its bound node's resources."""
    verify_calibration(
        profile,
        provider=settings.provider,
        purpose=settings.purpose,
        fingerprint=fingerprint,
    )
    measured = {row["function"]: row for row in profile.functions}
    if len(measured) != len(profile.functions) or set(measured) != set(functions):
        raise ValueError("profile function scope mismatch")
    for name, function in functions.items():
        row = measured[name]
        if (
            row["imageDigest"] != built.function(name, "rust").image.id
            or row["inputHash"]
            != "sha256:" + content_hash(canonical_bytes(function.input))
            or row["cpuQuota"] != function.cpu
            or row["memoryMiB"] != function.memory_mib
            or row["backend"] != "container-local"
            or row["runtime"] != "HTTP"
            or set(row["coLocation"]) != set(functions) - {name}
            or row["validity"]["minReplicas"] > 1
            or row["validity"]["maxReplicas"] < maxima[name]
        ):
            raise ValueError("profile runtime/input/resource scope mismatch")


def configuration_for_node(
    topology,
    settings: OneShotConfig,
    profile: CalibrationProfile | CalibrationProfiles,
    node_id: str,
    *,
    client: NanoFaasOneShotClient,
    cloud_uri: str,
    period: float,
    lead: float,
    epsilon: float,
    protocol: ProtocolSettings,
    anchor: datetime,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Translate declared identity and limits without deciding replicas or flows."""
    generations = client.status().catalog_generations
    profile = profile_for_node(profile, node_id)
    measured = {row["function"]: row for row in profile.functions}
    functions = {
        name: {
            "generation": generations[name],
            "imageDigest": measured[name]["imageDigest"],
            "inputHash": measured[name]["inputHash"],
            "utilization": function.utilization,
            "alpha": function.alpha,
            "delta": function.delta,
            "gamma": function.gamma,
        }
        for name, function in getattr(topology, "node_function_settings", {})
        .get(node_id, topology.function_settings)
        .items()
    }

    node = topology.resources.nodes[node_id]
    if settings.protocol_version == 2 and not session_id:
        raise ValueError("logical configuration requires a shared sessionId")
    return {
        "schemaVersion": settings.protocol_version,
        **({"sessionId": session_id} if settings.protocol_version == 2 else {}),
        "profileId": profile.profile_id,
        "environmentFingerprint": profile.environment_fingerprint,
        "purpose": settings.purpose,
        "allowSynthetic": False,
        "cloudUri": cloud_uri,
        "memoryCapacityMiB": node.config.memory_capacity_mib,
        "flowQuantum": settings.flow_quantum,
        "period": duration(period),
        "leadTime": duration(lead),
        "scheduled": False,
        "anchor": anchor.isoformat(),
        "preparationBudget": duration(protocol.preparation_seconds),
        "maxOperationalFraction": epsilon,
        "burst": 1,
        "functions": functions,
        "negotiation": {
            "auctionBudget": duration(protocol.auction_seconds),
            "peerTimeout": duration(protocol.peer_seconds),
            "solverBudget": duration(protocol.solver_seconds),
            "maxRounds": protocol.max_rounds,
            "parallelism": protocol.parallelism,
            "queueCapacity": protocol.queue_capacity,
            "maxPeers": protocol.max_peers,
            "maxAuctionFraction": epsilon,
        },
        "maxSolverStates": protocol.max_solver_states,
        "maxSolverBytes": protocol.max_solver_bytes,
    }


def configure_node(client, configuration, *, revision, output):
    """Retain the exact configuration and API refusal without retrying it."""
    append_observation(
        output,
        {
            "action": "configure",
            "node": client.base_url,
            "revision": revision,
            "configuration": configuration,
        },
    )
    try:
        return client.configure(configuration, revision=revision)
    except httpx.HTTPStatusError as error:
        append_observation(
            output,
            {
                "action": "configure-refused",
                "node": client.base_url,
                "status": error.response.status_code,
                "responseBody": error.response.text,
            },
        )
        raise


def upload_window(
    client: NanoFaasOneShotClient,
    *,
    node_id: str,
    epoch_start: datetime,
    epoch_end: datetime,
    rates: dict[str, float],
    revision: int,
) -> int:
    """Publish a single exact oracle window with current catalog generations."""
    generations = client.status().catalog_generations
    trace = {
        "schemaVersion": 1,
        "nodeId": node_id,
        "revision": revision + 1,
        "provider": "oracle",
        "producedAt": datetime.now(UTC).isoformat(),
        "entries": [
            {
                "function": name,
                "generation": generations[name],
                "start": epoch_start.isoformat(),
                "end": epoch_end.isoformat(),
                "rate": rate,
                "unit": "requests/s",
            }
            for name, rate in rates.items()
        ],
    }
    return client.load_trace(trace, revision=revision).revision


def scrape_metrics(http: httpx.Client, endpoint: str) -> dict[str, float]:
    """Read complete wall-time timer counters separately from parallel CPU timers."""
    from urllib.parse import urlsplit

    address = urlsplit(endpoint)
    response = http.get(
        f"http://{address.hostname}:9090/actuator/prometheus", timeout=5
    )
    response.raise_for_status()
    return parse_prometheus(response.text, (AUCTION_SUM, AUCTION_COUNT, SOLVER_SUM))


def prepare_parallel_epoch(
    clients: dict[str, NanoFaasOneShotClient],
    *,
    epoch: int,
    starts_at: datetime,
    ends_at: datetime,
    output: Path,
    session_id: str | None = None,
    activation_at: float | None = None,
    clock: ClockMonitor | None = None,
) -> tuple[WallTimingSample, dict[str, Any]]:
    """Trigger each node once; save failures/events and bound the full global wall."""
    if session_id is not None and (activation_at is None or clock is None):
        raise ValueError(
            "logical preparation needs a monotonic activation deadline and monitor"
        )
    before = {
        key: scrape_metrics(client.http, client.base_url)
        for key, client in clients.items()
    }
    barrier = Barrier(len(clients) + 1)
    launched = time.monotonic()
    activation_deadline = activation_at if activation_at is not None else launched
    evidence_lock = Lock()

    def prepare(binding):
        key, client = binding
        barrier.wait(timeout=10)
        began = time.monotonic()
        row: dict[str, Any] = {"triggerOffsetSeconds": began - launched}
        try:
            result = (
                client.prepare_epoch(epoch, starts_at=starts_at, ends_at=ends_at)
                if session_id is None
                else client.prepare_logical_epoch(
                    session_id,
                    epoch,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    activation_delay_seconds=activation_deadline - began,
                )
            )
            row["result"] = result.model_dump(mode="json", by_alias=True)
            row["complete"] = (
                result.status == "NEGOTIATED"
                if session_id is not None
                else result.status == "PREPARED"
                and result.desired_replicas == result.ready_replicas
            )
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            row.update(complete=False, error=str(error))
            if isinstance(error, httpx.HTTPStatusError):
                row.update(
                    statusCode=error.response.status_code,
                    responseBody=error.response.text,
                )
        row["readyOffsetSeconds"] = time.monotonic() - launched
        if session_id is not None:
            with evidence_lock:
                append_observation(
                    output.with_name(output.stem + "-negotiations.jsonl"),
                    {
                        "sessionId": session_id,
                        "epoch": epoch,
                        "node": key,
                        **row,
                    },
                )
        return key, row

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futures = [pool.submit(prepare, item) for item in clients.items()]
        barrier.wait(timeout=10)
        rows = dict(future.result() for future in futures)
    complete = all(row["complete"] for row in rows.values())
    negotiation_wall = max(row["readyOffsetSeconds"] for row in rows.values())
    activation_wall = 0.0
    if session_id is not None:
        for row in rows.values():
            row["negotiationResult"] = row.pop("result", None)
            row["negotiatedOffsetSeconds"] = row["readyOffsetSeconds"]
        if clock is None:
            raise ValueError("logical activation requires a clock monitor")
        wait_until_monotonic(activation_deadline, clock=clock)
        activation_started = time.monotonic()

        def activate(binding):
            key, client = binding
            row = rows[key]
            if not row["complete"]:
                return
            row["activationTriggerOffsetSeconds"] = time.monotonic() - launched
            try:
                result = client.activate_epoch(session_id, epoch)
                row["result"] = result.model_dump(mode="json", by_alias=True)
                row["complete"] = (
                    result.status == "PREPARED"
                    and result.desired_replicas == result.ready_replicas
                )
            except (httpx.HTTPError, ValueError, RuntimeError) as error:
                row.update(complete=False, error=str(error))
                if isinstance(error, httpx.HTTPStatusError):
                    row.update(
                        statusCode=error.response.status_code,
                        responseBody=error.response.text,
                    )
            row["readyOffsetSeconds"] = time.monotonic() - launched
            with evidence_lock:
                append_observation(
                    output.with_name(output.stem + "-activations.jsonl"),
                    {
                        "sessionId": session_id,
                        "epoch": epoch,
                        "node": key,
                        **row,
                    },
                )

        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            list(pool.map(activate, clients.items()))
        activation_wall = time.monotonic() - activation_started
        complete = all(row["complete"] for row in rows.values())
    for key, client in clients.items():
        row = rows[key]
        try:
            after = scrape_metrics(client.http, client.base_url)
            count = after.get(AUCTION_COUNT, 0) - before[key].get(AUCTION_COUNT, 0)
            if count != 1:
                raise ValueError("auction timer count did not advance exactly once")
            row["auctionSeconds"] = after[AUCTION_SUM] - before[key].get(AUCTION_SUM, 0)
            row["solverCpuSeconds"] = after.get(SOLVER_SUM, 0) - before[key].get(
                SOLVER_SUM, 0
            )
            pages = client.epoch_events(epoch)
            row["eventPages"] = [
                page.model_dump(mode="json", by_alias=True) for page in pages
            ]
            if not any(page.entries for page in pages):
                raise ValueError("no epoch event evidence")
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            row["collectionError"] = str(error)
            complete = False
    wall = max(row["readyOffsetSeconds"] for row in rows.values())
    operational_wall = negotiation_wall + activation_wall
    observation = {
        "epoch": epoch,
        "startsAt": starts_at.isoformat(),
        "endsAt": ends_at.isoformat(),
        "nodes": rows,
        "censored": not complete,
        "globalReadyWallSeconds": wall,
        "auctionWallUpperBoundSeconds": operational_wall if complete else None,
        "method": "parallel-trigger-to-all-ready-conservative-auction-upper-bound",
    }
    if session_id is not None:
        observation.update(
            protocolVersion=2,
            sessionId=session_id,
            globalOperationalWallSeconds=operational_wall,
            negotiationWallSeconds=negotiation_wall,
            activationWallSeconds=activation_wall,
            readinessLagSeconds=max(0.0, launched + wall - activation_deadline),
            method="parallel-logical-negotiation-plus-activation-excluding-scheduled-wait",
        )
    append_observation(output, observation)
    return WallTimingSample(
        auction_seconds=operational_wall if complete else None,
        ready_seconds=operational_wall if complete else None,
        censored=not complete,
    ), observation


def wait_until_monotonic(deadline: float, *, clock: ClockMonitor) -> None:
    """Wait against one host monotonic deadline while checking owned collection."""
    while (remaining := deadline - time.monotonic()) > 0:
        clock.require_healthy()
        time.sleep(min(0.25, remaining))


def wait_until(instant: datetime, *, clock: ClockMonitor) -> None:
    """Respect real epoch reservations while checking clock health in bounded waits."""
    while (remaining := (instant - datetime.now(UTC)).total_seconds()) > 0:
        clock.require_healthy()
        time.sleep(min(0.25, remaining))


class QualifyTimingTask(Task[TimingQualification]):
    """Measure each explicit workload cell without modifying service calibration."""

    title = "Qualify local one-shot wall time and readiness"

    def __init__(
        self,
        topology: Any,
        settings: OneShotConfig,
        profile: CalibrationProfile | CalibrationProfiles,
        *,
        clock: Resource[ClockMonitor],
        run_dir: Path,
    ):
        """Bind the frozen prerequisite and matrix under the topology's resources."""
        self.topology, self.settings, self.profile = topology, settings, profile
        self.clock, self.run_dir = clock, run_dir

    def run(self, inputs: TaskInputs) -> TaskOutcome[TimingQualification]:
        """Publish measured qualification artifacts, preserving censored evidence."""
        timing = self.settings.timing
        if timing is None or self.settings.profile is None:
            raise ValueError("explicit timing matrix and profile are required")
        if (
            isinstance(self.profile, CalibrationProfiles)
            and self.profile.concurrent_capacities is not None
        ):
            from nanolab.one_shot.profiles import synchronized_timing_trace

            synchronized_timing_trace(self.settings, self.profile)
        built = inputs.resource(self.topology.distribution)
        fingerprint = verify_profile_scope(
            self.profile,
            self.settings,
            self.topology,
            inputs.upstream(),
            built,
        )
        budget = timing.protocol.auction_seconds + timing.protocol.preparation_seconds
        preliminary = [
            candidate
            for candidate in timing.period_candidates
            if budget <= timing.epsilon * candidate
            and timing.lead_seconds < candidate
            and candidate <= timing.max_trace_resolution
            and budget < timing.lead_seconds
        ]
        if not preliminary:
            raise ValueError(
                "no measurement period fits explicit protocol/lead budgets"
            )
        period = min(preliminary)
        cloud_key = next(
            key
            for key, node in self.topology.resources.nodes.items()
            if node.config.kind == "cloud"
        )
        cloud_uri = inputs.resource(self.topology.endpoints[cloud_key])
        monitor = inputs.resource(self.clock)
        all_samples, cells = [], []
        epoch = 0
        session_id = uuid4().hex if self.settings.protocol_version == 2 else None
        grid_anchor = datetime.now(UTC)
        with httpx.Client(trust_env=False) as http:
            if self.settings.purpose == "scientific-experiment":
                from nanolab.one_shot.capacity import comparison_replica_vectors

                functions = self.topology.node_function_settings[cloud_key]
                cloud = self.topology.resources.nodes[cloud_key]
                static = (
                    comparison_replica_vectors(
                        self.settings, self.topology.function_aliases
                    )[cloud_key]
                    if self.settings.strict_memory and len(self.settings.functions) > 1
                    else {name: fn.max_replicas for name, fn in functions.items()}
                )
                for name in functions:
                    http.put(
                        cloud_uri + f"/v1/functions/{name}/replicas",
                        json={"replicas": static[name]},
                        timeout=30,
                    ).raise_for_status()
                    for replica in range(1, static[name] + 1):
                        deadline = time.monotonic() + 30
                        while True:
                            try:
                                runtime, observed = runtime_endpoint(
                                    cloud, name, inputs=inputs, replica=replica
                                )
                                response = http.get(
                                    runtime + "/runtime/status", timeout=2
                                )
                                response.raise_for_status()
                                status = response.json()
                                if (
                                    status.get("maxConcurrentHandlers") != 1
                                    or status.get("physicalReleaseProof") is not True
                                ):
                                    raise ValueError(
                                        "cloud replica lacks physical c1 readiness"
                                    )
                                break
                            except (httpx.HTTPError, ValueError, RuntimeError):
                                if time.monotonic() >= deadline:
                                    raise
                                time.sleep(0.1)
                        ready = {
                            "node": cloud_key,
                            "function": name,
                            "replica": replica,
                            "url": runtime,
                            "container": observed,
                            "status": status,
                        }
                        if self.settings.strict_memory:
                            from nanolab.tasks.one_shot.preflight import (
                                collect_container_cgroup,
                                validate_strict_cgroup,
                            )

                            fn = functions[name]
                            sample = collect_container_cgroup(cloud, observed)
                            ready["cgroup"] = sample
                            append_observation(
                                self.run_dir / "cloud-readiness.jsonl", ready
                            )
                            if (
                                observed.get("HostConfig", {}).get("MemorySwap")
                                != fn.memory_mib * 1024 * 1024
                            ):
                                raise ValueError("cloud strict swap limit mismatch")
                            validate_strict_cgroup(
                                sample, memory_mib=fn.memory_mib, cpu=fn.cpu
                            )
                        else:
                            append_observation(
                                self.run_dir / "cloud-readiness.jsonl", ready
                            )
            clients = {
                key: NanoFaasOneShotClient(inputs.resource(endpoint), http=http)
                for key, endpoint in self.topology.endpoints.items()
                if self.topology.resources.nodes[key].config.kind == "edge"
            }
            revisions = dict.fromkeys(clients, 0)
            for key, client in clients.items():
                client.load_profile(profile_for_node(self.profile, key), revision=0)
                configuration = configuration_for_node(
                    self.topology,
                    self.settings,
                    self.profile,
                    key,
                    client=client,
                    cloud_uri=cloud_uri,
                    period=period,
                    lead=timing.lead_seconds,
                    epsilon=timing.epsilon,
                    protocol=timing.protocol,
                    anchor=grid_anchor,
                    session_id=session_id,
                )
                configure_node(
                    client,
                    configuration,
                    revision=client.status().revision,
                    output=self.run_dir / "api.jsonl",
                )
            for cell_number, cell in enumerate(timing.matrix):
                required_nodes = (
                    set(self.topology.endpoints)
                    if self.settings.purpose == "scientific-experiment"
                    else set(clients)
                )
                if set(cell.rates) != required_nodes:
                    raise ValueError("timing matrix must declare every load origin")
                samples = []
                measured_through = time.time()
                with QualificationLoad(
                    self.topology,
                    self.settings,
                    inputs,
                    cell=cell,
                    period=period,
                    run_dir=self.run_dir / f"load-cell-{cell_number}",
                    clock=monitor,
                ) as loaded:
                    for number in range(timing.minimum_samples):
                        monitor.require_healthy()
                        loaded.require_healthy()
                        activation_at = time.monotonic() + timing.lead_seconds
                        starts = (
                            grid_anchor + timedelta(seconds=epoch * period)
                            if session_id is not None
                            else datetime.now(UTC)
                            + timedelta(seconds=timing.lead_seconds)
                        )
                        ends = starts + timedelta(seconds=period)
                        for key, client in clients.items():
                            rates = {
                                self.topology.function_aliases[name]: rate
                                for name, rate in cell.rates[key].items()
                            }
                            if set(rates) != set(self.topology.function_settings):
                                raise ValueError(
                                    "timing matrix must declare every function"
                                )
                            if any(
                                abs(
                                    rate / self.settings.flow_quantum
                                    - round(rate / self.settings.flow_quantum)
                                )
                                > 1e-9
                                for rate in rates.values()
                            ):
                                raise ValueError(
                                    "oracle rate is not representable on flowQuantum"
                                )
                            revisions[key] = upload_window(
                                client,
                                node_id=key,
                                epoch_start=starts,
                                epoch_end=ends,
                                rates=rates,
                                revision=revisions[key],
                            )
                        sample, observation = prepare_parallel_epoch(
                            clients,
                            epoch=epoch,
                            starts_at=starts,
                            ends_at=ends,
                            output=self.run_dir / "timing-samples.jsonl",
                            **(
                                {
                                    "session_id": session_id,
                                    "activation_at": activation_at,
                                    "clock": monitor,
                                }
                                if session_id is not None
                                else {}
                            ),
                        )
                        append_observation(
                            self.run_dir / "matrix.jsonl",
                            {"cell": cell.id, "number": number, **observation},
                        )
                        if sample.censored:
                            capture_replica_diagnostics(
                                self.topology,
                                inputs,
                                self.run_dir / "replica-diagnostics.jsonl",
                                epoch=epoch,
                            )
                        measured_through = time.time()
                        samples.append(sample)
                        epoch += 1
                        if session_id is not None:
                            wait_until_monotonic(
                                activation_at + period + 0.1, clock=monitor
                            )
                        else:
                            wait_until(
                                ends + timedelta(milliseconds=100), clock=monitor
                            )
                    load = loaded.audit(cutoff=measured_through)
                result = select_timing(
                    samples,
                    period_candidates=timing.period_candidates,
                    max_trace_resolution=timing.max_trace_resolution,
                    minimum_samples=timing.minimum_samples,
                    quantile=timing.quantile,
                    margin_seconds=timing.margin_seconds,
                    ready_margin_seconds=timing.ready_margin_seconds,
                    epsilon=timing.epsilon,
                    protocol_budget_seconds=budget,
                    provider=self.settings.provider,
                    purpose=self.settings.purpose,
                    fingerprint=fingerprint,
                    profile_sha256=self.settings.profile.sha256,
                )
                result = result.model_copy(
                    update={"protocol_version": self.settings.protocol_version}
                )
                if not load["valid"]:
                    result = result.model_copy(
                        update={
                            "qualified": False,
                            "reason": "NOT_QUALIFIED: actual handler load failed",
                        }
                    )
                cells.append(
                    {
                        "cell": cell.model_dump(),
                        "result": result.model_dump(),
                        "load": load,
                    }
                )
                all_samples.extend(samples)
            for client in clients.values():
                if not client.drain_and_release():
                    raise RuntimeError("epoch replica release unconfirmed")
        result = select_timing(
            all_samples,
            period_candidates=timing.period_candidates,
            max_trace_resolution=timing.max_trace_resolution,
            minimum_samples=timing.minimum_samples,
            quantile=timing.quantile,
            margin_seconds=timing.margin_seconds,
            ready_margin_seconds=timing.ready_margin_seconds,
            epsilon=timing.epsilon,
            protocol_budget_seconds=budget,
            provider=self.settings.provider,
            purpose=self.settings.purpose,
            fingerprint=fingerprint,
            profile_sha256=self.settings.profile.sha256,
        )
        # Every matrix cell must qualify independently. The common settings cover
        # the worst cell, including readiness, rather than averaging difficult cells.
        values = [TimingQualification.model_validate(cell["result"]) for cell in cells]
        result = TimingQualification.model_validate(
            {
                **result.model_dump(),
                "qualified": result.qualified
                and all(value.qualified for value in values),
                "period_seconds": max(value.period_seconds for value in values),
                "lead_seconds": max(value.lead_seconds for value in values),
                "quantile_seconds": max(value.quantile_seconds for value in values),
                "protocol": timing.protocol.model_dump(by_alias=True),
                "protocol_version": self.settings.protocol_version,
                "matrix": cells,
                "reason": None
                if all(value.qualified for value in values)
                else "NOT_QUALIFIED: a matrix cell failed",
            }
        )
        digest = write_immutable_artifact(
            self.run_dir / "timing.json", canonical_bytes(result.model_dump())
        )
        write_immutable_artifact(
            self.run_dir / "timing-reference.json",
            canonical_bytes(
                {
                    "path": str(self.run_dir / "timing.json"),
                    "sha256": digest,
                }
            ),
        )
        if not result.qualified:
            raise ValueError(result.reason)
        return TaskOutcome(value=result)
