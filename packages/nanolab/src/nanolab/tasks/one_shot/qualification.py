"""Measured manual preparation through NanoFaaS APIs; no auction decisions here."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import httpx
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome

from nanolab.config.one_shot import OneShotConfig, ProtocolSettings
from nanolab.one_shot.client import NanoFaasOneShotClient
from nanolab.one_shot.models import (
    CalibrationProfile,
    TimingQualification,
    verify_calibration,
)
from nanolab.tasks.loadtest.soak import parse_prometheus
from nanolab.tasks.one_shot.artifacts import (
    append_observation,
    canonical_bytes,
    content_hash,
    write_immutable_artifact,
)
from nanolab.tasks.one_shot.calibration import environment_identity
from nanolab.tasks.one_shot.clock import ClockMonitor
from nanolab.tasks.one_shot.preflight import runtime_endpoint
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


def read_profile(settings: OneShotConfig) -> CalibrationProfile:
    """Reject altered or synthetic prerequisite bytes before any VM acquisition."""
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
    verify_calibration(
        profile,
        provider=settings.provider,
        purpose=settings.purpose,
        fingerprint=fingerprint,
    )
    measured = {row["function"]: row for row in profile.functions}
    if len(measured) != len(profile.functions) or set(measured) != set(
        topology.function_settings
    ):
        raise ValueError("profile function scope mismatch")
    for name, function in topology.function_settings.items():
        row = measured[name]
        if (
            row["imageDigest"] != built.function(name, "rust").image.id
            or row["inputHash"]
            != "sha256:" + content_hash(canonical_bytes(function.input))
            or row["cpuQuota"] != function.cpu
            or row["memoryMiB"] != function.memory_mib
            or row["backend"] != "container-local"
            or row["runtime"] != "HTTP"
            or set(row["coLocation"]) != set(topology.function_settings) - {name}
            or row["validity"]["minReplicas"] > 1
            or row["validity"]["maxReplicas"]
            < max(
                node.memory_capacity_mib // function.memory_mib
                for node in settings.nodes
            )
        ):
            raise ValueError("profile runtime/input/resource scope mismatch")
    return fingerprint


def configuration_for_node(
    topology,
    settings: OneShotConfig,
    profile: CalibrationProfile,
    node_id: str,
    *,
    client: NanoFaasOneShotClient,
    cloud_uri: str,
    period: float,
    lead: float,
    epsilon: float,
    protocol: ProtocolSettings,
    anchor: datetime,
) -> dict[str, Any]:
    """Translate declared identity and limits without deciding replicas or flows."""
    generations = client.status().catalog_generations
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
        for name, function in topology.function_settings.items()
    }

    node = topology.resources.nodes[node_id]
    return {
        "schemaVersion": 1,
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
) -> tuple[WallTimingSample, dict[str, Any]]:
    """Trigger each node once; save failures/events and bound the full global wall."""
    before = {
        key: scrape_metrics(client.http, client.base_url)
        for key, client in clients.items()
    }
    barrier = Barrier(len(clients) + 1)
    launched = time.monotonic()

    def prepare(binding):
        key, client = binding
        barrier.wait(timeout=10)
        began = time.monotonic()
        row: dict[str, Any] = {"triggerOffsetSeconds": began - launched}
        try:
            result = client.prepare_epoch(epoch, starts_at=starts_at, ends_at=ends_at)
            row["result"] = result.model_dump(mode="json", by_alias=True)
            row["complete"] = (
                result.status == "PREPARED"
                and result.desired_replicas == result.ready_replicas
            )
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            row.update(complete=False, error=str(error))
        row["readyOffsetSeconds"] = time.monotonic() - launched
        return key, row

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futures = [pool.submit(prepare, item) for item in clients.items()]
        barrier.wait(timeout=10)
        rows = dict(future.result() for future in futures)
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
    observation = {
        "epoch": epoch,
        "startsAt": starts_at.isoformat(),
        "endsAt": ends_at.isoformat(),
        "nodes": rows,
        "censored": not complete,
        "globalReadyWallSeconds": wall,
        "auctionWallUpperBoundSeconds": wall if complete else None,
        "method": "parallel-trigger-to-all-ready-conservative-auction-upper-bound",
    }
    append_observation(output, observation)
    return WallTimingSample(
        auction_seconds=wall if complete else None,
        ready_seconds=wall if complete else None,
        censored=not complete,
    ), observation


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
        profile: CalibrationProfile,
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
        with httpx.Client(trust_env=False) as http:
            clients = {
                key: NanoFaasOneShotClient(inputs.resource(endpoint), http=http)
                for key, endpoint in self.topology.endpoints.items()
                if self.topology.resources.nodes[key].config.kind == "edge"
            }
            revisions = dict.fromkeys(clients, 0)
            for key, client in clients.items():
                client.load_profile(self.profile, revision=0)
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
                    anchor=datetime.now(UTC),
                )
                configure_node(
                    client,
                    configuration,
                    revision=client.status().revision,
                    output=self.run_dir / "api.jsonl",
                )
            for cell in timing.matrix:
                if set(cell.rates) != set(clients):
                    raise ValueError("timing matrix must declare every edge")
                samples = []
                for number in range(timing.minimum_samples):
                    monitor.require_healthy()
                    starts = datetime.now(UTC) + timedelta(seconds=timing.lead_seconds)
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
                    samples.append(sample)
                    epoch += 1
                    wait_until(ends + timedelta(milliseconds=100), clock=monitor)
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
                cells.append({"cell": cell.model_dump(), "result": result.model_dump()})
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
