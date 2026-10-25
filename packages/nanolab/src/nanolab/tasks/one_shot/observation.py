"""Sonata tasks for an explicitly unqualified observation."""

import hashlib
import json
import math
import statistics
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import httpx
from pydantic import JsonValue
from sonata_engine import Task, TaskOutcome

from nanolab.config.one_shot import OperationalLimits, operational_limit
from nanolab.one_shot.capacity import comparison_replica_vectors
from nanolab.one_shot.client import NanoFaasOneShotClient
from nanolab.one_shot.models import CalibrationProfile, CampaignManifest
from nanolab.one_shot.profiles import CalibrationProfiles
from nanolab.one_shot.report import render_report
from nanolab.one_shot.returned_errors import summarize_responses
from nanolab.one_shot.supervision import (
    CampaignSupervision,
    check_sampler,
)
from nanolab.one_shot.vm_resources import (
    VmTelemetrySession,
    read_samples,
    telemetry_policy,
)
from nanolab.tasks.one_shot.arrivals import (
    read_frozen_arrival_profile,
    retime_schedule,
    schedule_from_arrival_profile,
)
from nanolab.tasks.one_shot.artifacts import (
    canonical_bytes,
    write_immutable_artifact,
)
from nanolab.tasks.one_shot.calibration import measure_invocation, workload_checksum
from nanolab.tasks.one_shot.experiment import (
    collect_original_evidence,
    generator_rows,
    logical_schedule,
)
from nanolab.tasks.one_shot.forecast_errors import record_forecast_errors
from nanolab.tasks.one_shot.preflight import (
    collect_container_cgroup,
    runtime_endpoint,
    validate_strict_cgroup,
)
from nanolab.tasks.one_shot.qualification import configuration_for_node, configure_node
from nanolab.tasks.one_shot.trace import (
    oracle_entries,
    request_schedule,
    warmup_trace,
)

SCHEDULE_SETUP_SECONDS = 90


def oracle_lifetime_seconds(trace, *, warmup_seconds, limits=None):
    """Keep a once-uploaded trace valid for the complete frozen load horizon."""
    limits = limits or OperationalLimits()
    return math.ceil(
        warmup_seconds
        + limits.schedule_setup_seconds
        + len(trace.windows) * trace.period_seconds
        + limits.generator_drain_seconds
    )


FORECASTERS = ("ewma", "holt", "ari")
HOLT_WEIGHTS = ("level", "trend", "damping")


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unit(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 < value <= 1
    )


def observation_forecast(protocol: dict) -> dict:
    """Return the frozen forecast source: ORACLE unless the freeze selected another."""
    forecast = protocol.get("forecast")
    if forecast is None:
        return {"provider": "oracle", "maxAgeSeconds": protocol["oracleMaxAgeSeconds"]}
    provider = forecast.get("provider")
    window, age = forecast.get("windowSeconds"), forecast.get("maxAgeSeconds")
    if provider not in FORECASTERS or not (
        _whole(window) and _whole(age) and 0 < window < age
    ):
        raise ValueError(
            "a forecast needs ewma, holt or ari, a whole-second window "
            "and a longer max age"
        )
    # NanoFaaS keeps 4 to 100000 complete windows for Holt and ARI.
    history = forecast.get("historyWindows")
    order = forecast.get("order")
    holt = forecast.get("holt") or {}
    if provider == "ewma" and not _unit(forecast.get("alpha")):
        raise ValueError("an EWMA forecast needs alpha in (0, 1]")
    if provider != "ewma" and not (_whole(history) and 4 <= history <= 100_000):
        raise ValueError(f"a {provider} forecast needs a history of 4-100000 windows")
    if provider == "holt" and not all(_unit(holt.get(k)) for k in HOLT_WEIGHTS):
        raise ValueError("a holt forecast needs level, trend and damping in (0, 1]")
    if provider == "ari" and not (
        _whole(order) and 1 <= order <= 64 and 2 * order + 2 <= history
    ):
        raise ValueError(
            "an ari forecast needs an order in [1, 64] within (history - 2) / 2"
        )
    return forecast


def forecast_properties(forecast: dict) -> dict[str, str]:
    """NanoFaaS forecasting properties beyond provider, window and max age."""
    provider = forecast["provider"]
    if provider == "ewma":
        return {"alpha": repr(forecast["alpha"])}
    if provider == "holt":
        return {
            "history": str(forecast["historyWindows"]),
            **{f"holt.{k}": repr(forecast["holt"][k]) for k in HOLT_WEIGHTS},
        }
    if provider == "ari":
        return {
            "history": str(forecast["historyWindows"]),
            "ari.order": str(forecast["order"]),
        }
    return {}


def observation_purpose(mode: str) -> str:
    """Keep the ORACLE observation's historical name; name the others apart."""
    return (
        "first-functional-oracle-observation"
        if mode == "oracle"
        else f"{mode}-forecast-observation"
    )


def sha(path):
    """Hash saved bytes incrementally."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    """Never replace retained evidence."""
    return write_immutable_artifact(path, canonical_bytes(value))


def event(directory, stage, **values):
    """Persist a stage once and route it through the current campaign if present."""
    from sonata_engine.workflow.context import active_sink

    from nanolab.one_shot.recording import CampaignRecorder

    row = {"atUtc": datetime.now(UTC).isoformat(), "stage": stage, **values}
    sink = active_sink()
    if (
        isinstance(sink, CampaignSupervision)
        and sink.output.resolve() == directory.resolve()
    ):
        sink.progress(row)
    else:
        CampaignRecorder(directory).append_progress(row)


def source_file_names(root: Path) -> list[str]:
    """Inventory tracked and untracked nonignored runtime inputs."""
    names = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        text=True,
    ).split("\0")
    return sorted({name for name in names if name and (root / name).is_file()})


def verify_files(root, frozen):
    """Reject drift before VM acquisition and before starting requests."""
    for name, expected in frozen["files"].items():
        if sha(root / name) != expected:
            raise ValueError("frozen bytes changed: " + name)
    if frozen.get("sourceInventoryComplete") and set(
        source_file_names(Path(frozen["source"]))
    ) != set(frozen["sourceFiles"]):
        raise ValueError("external source inventory changed")
    for name, expected in frozen.get("sourceFiles", {}).items():
        if sha(Path(frozen["source"]) / name) != expected:
            raise ValueError("external runtime asset changed: " + name)
    for name, expected in frozen.get("dependencyFiles", {}).items():
        if sha(Path(name)) != expected:
            raise ValueError("workflow dependency changed: " + name)


def native_profile(
    node,
    parameters,
    historical,
    images,
    *,
    revision,
    fingerprint,
    physical_sha,
    replicas,
):
    """Keep real statistics and mark prospective adaptations as unqualified."""
    rows = []
    for name, fn in parameters.items():
        observed = historical[name]
        rows.append(
            {
                "function": name,
                "imageDigest": images[name],
                "runtime": "HTTP",
                "backend": "container-local",
                "inputHash": "sha256:"
                + hashlib.sha256(canonical_bytes(fn["input"])).hexdigest(),
                "cpuQuota": fn["cpu"],
                "memoryMiB": fn["memoryMiB"],
                "replicas": replicas,
                "coLocation": sorted(set(parameters) - {name}),
                "serviceSeconds": observed["meanSeconds"],
                "statistics": {
                    k: observed[k]
                    for k in [
                        "sampleCount",
                        "meanSeconds",
                        "stddevSeconds",
                        "p95Seconds",
                    ]
                },
                "validity": {
                    "minReplicas": 1,
                    "maxReplicas": fn["maxReplicas"],
                    "maxRelativeCapacityError": 0.35,
                },
                "measurement": {
                    "warmupInvocations": 1,
                    "includesColdStarts": False,
                    "occupancy": "physical-handler",
                    "rawSamplesHash": "sha256:" + physical_sha,
                },
            }
        )
    return CalibrationProfile.model_validate(
        {
            "schemaVersion": 1,
            "profileId": "historical-observation-" + node,
            "provider": "multipass",
            "purpose": "workflow-validation",
            "synthetic": True,
            "sourceCommit": revision,
            "environmentFingerprint": fingerprint,
            "environment": {
                "host": "local-controlled",
                "vm": "multipass",
                "os": "linux",
                "architecture": "aarch64",
                "cpu": node,
            },
            "functions": rows,
        }
    )


class WarmInstances(Task):
    """Warm the balanced vectors and validate actual kernel quotas."""

    title = "Warm balanced function instances"

    def __init__(self, run):
        """Bind the frozen observation context."""
        self.run_context = run

    def run(self, inputs):
        """Execute this observation stage on the owned resources."""
        run = self.run_context
        counts = comparison_replica_vectors(run.settings, run.topology.function_aliases)
        with httpx.Client(
            trust_env=False, timeout=operational_limit(run.settings, "api_seconds", 30)
        ) as http:
            for node, vector in counts.items():
                endpoint = inputs.resource(run.topology.endpoints[node])
                for name, count in vector.items():
                    fn = run.topology.node_function_settings[node][name]
                    http.put(
                        endpoint + f"/v1/functions/{name}/replicas",
                        json={"replicas": count},
                    ).raise_for_status()
                    for replica in range(1, count + 1):
                        deadline = time.monotonic() + operational_limit(
                            run.settings, "replica_start_seconds", 120
                        )
                        while True:
                            try:
                                url, container = runtime_endpoint(
                                    run.topology.resources.nodes[node],
                                    name,
                                    inputs=inputs,
                                    replica=replica,
                                )
                                status = http.get(url + "/runtime/status")
                                status.raise_for_status()
                                if (
                                    status.json().get("physicalReleaseProof")
                                    is not True
                                    or status.json().get("maxConcurrentHandlers") != 1
                                ):
                                    raise ValueError("SDK release proof / c1 missing")
                                break
                            except (httpx.HTTPError, RuntimeError):
                                if time.monotonic() >= deadline:
                                    raise
                                time.sleep(0.2)
                        result = measure_invocation(
                            http,
                            url,
                            payload=fn.input,
                            execution_id=f"bootstrap:{node}:{name}:{replica}",
                            expected_output=workload_checksum(fn.input, function=name),
                            timeout_seconds=operational_limit(
                                run.settings, "bootstrap_invocation_seconds", 30
                            ),
                            release_timeout_seconds=operational_limit(
                                run.settings, "bootstrap_invocation_seconds", 30
                            ),
                        )
                        if result["state"] != "complete":
                            raise RuntimeError("physical warm invocation incomplete")
                        cgroup = collect_container_cgroup(
                            run.topology.resources.nodes[node], container
                        )
                        validate_strict_cgroup(
                            cgroup,
                            memory_mib=fn.memory_mib,
                            cpu=fn.cpu,
                            require_no_swap=False,
                        )
                        save(
                            run.run_dir / f"{node}-{name}-r{replica}-warm.json",
                            {"invocation": result, "cgroup": cgroup},
                        )
                event(
                    run.run_dir,
                    "instances-warm",
                    node=node,
                    replicas=sum(vector.values()),
                )
        return TaskOutcome()


class ObserverPrecheck(Task):
    """Record idle collector/sampler cost before warmup and measurement."""

    title = "Measure observer resource cost"

    def __init__(self, run):
        """Bind the frozen observation context."""
        self.run_context = run

    def run(self, inputs):
        """Execute this observation stage on the owned resources."""
        run = self.run_context
        directory = run.run_dir / "observer-precheck"
        directory.mkdir()
        anchor = datetime.now(UTC) + timedelta(seconds=30)
        mono = time.monotonic() + 30
        grid = {
            "protocolVersion": 2,
            "sessionId": uuid4().hex,
            "hostBootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "anchorMonotonicSeconds": mono,
            "probeScope": "parent",
            "probeParentLevels": 1,
        }
        save(
            directory / "manifest.json",
            {
                "anchor": anchor.isoformat(),
                "period_seconds": 60,
                "run_id": "observer-precheck",
                "parameters": {
                    "generator": {"warmupSeconds": 0},
                    "logicalSchedule": grid,
                },
            },
        )
        context = SimpleNamespace(
            run_dir=directory,
            topology=run.topology,
            settings=run.settings,
            mode="oracle",
            trace=SimpleNamespace(windows=[{}], period_seconds=60),
        )
        context.supervision = getattr(run, "supervision", None)
        session = VmTelemetrySession(
            context, inputs, terminal_probe=inputs.resource(run.clock).refresh
        )
        supervision = context.supervision
        try:
            if supervision is not None:
                supervision.watch(
                    "precheck-sampler", lambda: check_sampler(session), writer=session
                )
            event(run.run_dir, "observer-precheck-started", seconds=60)
            while time.monotonic() < mono + 60:
                session.require_healthy()
                inputs.resource(run.clock).require_healthy()
                time.sleep(0.2)
        finally:
            try:
                if supervision is not None:
                    supervision.unwatch("precheck-sampler")
            finally:
                session.close()
        costs = {}
        for node in run.topology.resources.nodes:
            rows, malformed = read_samples(directory / "vm-resources" / f"{node}.jsonl")
            cpu = [
                r["sampler"]["cpuUsedCores"]
                for r in rows
                if r.get("sampler", {}).get("cpuUsedCores") is not None
            ]
            costs[node] = {
                "samples": len(rows),
                "malformedLines": malformed,
                "samplerMeanUsedCores": statistics.mean(cpu) if cpu else None,
                "samplerPeakRssBytes": max(
                    (r.get("sampler", {}).get("memoryRssPeakBytes", 0) for r in rows),
                    default=0,
                ),
            }
        save(
            directory / "cost.json",
            {
                "method": "idle-sampler-and-collector-resource-cost",
                "qualified": False,
                "threshold": None,
                "nodes": costs,
                "collectorCounters": "container series in vm-resources raw files",
                "notEstimated": "paired effect on function goodput/latency",
            },
        )
        event(run.run_dir, "observer-precheck-complete", costs=costs)
        return TaskOutcome()


class ObservationFreeze(Task):
    """Freeze schedule and adapted profiles after actual resource verification."""

    title = "Freeze unqualified observation schedule"

    def __init__(self, run):
        """Bind the frozen observation context."""
        self.run_context = run

    def run(self, inputs):
        """Execute this observation stage on the owned resources."""
        run = self.run_context
        verify_files(run.frozen_dir, run.frozen)
        built = inputs.resource(run.topology.distribution)
        images = {c.name: c.image.id for c in built.components}
        observed = json.loads((run.frozen_dir / "observations.json").read_text())
        historical = json.loads((run.frozen_dir / "statistics.json").read_text())
        profiles = {
            node: native_profile(
                node,
                {fn: s.model_dump(by_alias=True) for fn, s in parameters.items()},
                historical[node],
                images,
                revision=built.source["revision"],
                fingerprint="sha256:" + sha(run.frozen_dir / "freeze.json"),
                physical_sha=observed["sourcePhysicalSha256"],
                replicas=observed["measuredReplicaCountsPerFunction"][node],
            )
            for node, parameters in run.topology.node_function_settings.items()
        }
        profile_sha = save(
            run.run_dir / "native-profiles.json",
            {
                "qualified": False,
                "purpose": "adapted-historical-observation-profiles",
                "sourceScope": run.frozen["serviceScope"],
                "profiles": {
                    n: p.model_dump(by_alias=True) for n, p in profiles.items()
                },
            },
        )
        run.profile = CalibrationProfiles(
            profiles, profile_sha, "sha256:" + sha(run.frozen_dir / "freeze.json")
        )
        setup = operational_limit(
            run.settings, "schedule_setup_seconds", SCHEDULE_SETUP_SECONDS
        )
        anchor = datetime.now(UTC) + timedelta(
            seconds=run.settings.experiment.warmup_seconds + setup
        )
        mono = time.monotonic() + run.settings.experiment.warmup_seconds + setup
        protocol = json.loads((run.frozen_dir / "operator-protocol.json").read_text())
        parameters = {
            "observationOnly": True,
            "vmTelemetry": telemetry_policy(),
            "operationalLimits": run.settings.operational_limits.model_dump(
                by_alias=True
            ),
            "scientificQualified": False,
            "profileScope": run.frozen["serviceScope"],
            "generator": {
                "warmupSeconds": run.settings.experiment.warmup_seconds,
                "vus": 512,
                "maxArrivalLatenessSeconds": 3600,
            },
            "logicalSchedule": {
                "protocolVersion": 2,
                "sessionId": uuid4().hex,
                "hostBootId": run.frozen["hostBootId"],
                "anchorMonotonicSeconds": mono,
                "epochCount": len(run.trace.windows),
            },
            "nodes": [n.model_dump(by_alias=True) for n in run.settings.nodes],
            "functions": {
                fn: s.model_dump(by_alias=True)
                for fn, s in run.topology.function_settings.items()
            },
            "protocol": protocol["protocol"],
            "timing": protocol,
            "images": images,
            "initialReplicaCounts": comparison_replica_vectors(
                run.settings, run.topology.function_aliases
            ),
            "observerPrecheckSha256": sha(run.run_dir / "observer-precheck/cost.json"),
            "softwareFreezeSha256": sha(run.frozen_dir / "freeze.json"),
        }
        arrival_document = read_frozen_arrival_profile(
            run.frozen_dir,
            run.frozen,
            warmup_seconds=run.settings.experiment.warmup_seconds,
        )
        if arrival_document is not None:
            parameters["generator"]["arrivalProfile"] = run.frozen["arrivalProfile"]
        manifest = CampaignManifest(
            provider="multipass",
            purpose="workflow-validation",
            environment_fingerprint=run.profile.environment_fingerprint,
            source_commit=built.source["revision"],
            profile_sha256=profile_sha,
            qualification_sha256=sha(run.frozen_dir / "operator-protocol.json"),
            trace_sha256=run.trace.sha256,
            seed=run.trace.seed,
            nodes=run.trace.nodes,
            functions=run.trace.functions,
            period_seconds=run.trace.period_seconds,
            lead_seconds=protocol["leadSeconds"],
            flow_quantum=run.trace.flow_quantum,
            modes=[run.mode],
            mode=run.mode,
            run_id="observation:" + run.trace.sha256[:12],
            anchor=anchor.isoformat(),
            parameters=parameters,
        )
        endpoints = {n: inputs.resource(r) for n, r in run.topology.endpoints.items()}
        if arrival_document is not None and arrival_document["schemaVersion"] == 2:
            schedule = schedule_from_arrival_profile(
                run.trace,
                arrival_document,
                anchor=anchor,
                endpoints=endpoints,
                run_id=manifest.run_id,
            )
            scheduled = [row for row in schedule if row["phase"] == "campaign"]
            warm = [row for row in schedule if row["phase"] == "warmup"]
        else:
            scheduled = request_schedule(
                run.trace, anchor=anchor, endpoints=endpoints, run_id=manifest.run_id
            )
            warm = request_schedule(
                warmup_trace(run.trace, seconds=run.settings.experiment.warmup_seconds),
                anchor=anchor
                - timedelta(seconds=run.settings.experiment.warmup_seconds),
                endpoints=endpoints,
                run_id=manifest.run_id + ":warmup",
            )
            warm = [dict(row, phase="warmup", epoch=-1) for row in warm]
            schedule = sorted(
                warm + scheduled, key=lambda r: (r["scheduledAt"], r["originalId"])
            )
            if arrival_document is not None:
                schedule = retime_schedule(
                    schedule, arrival_document, anchor_timestamp=anchor.timestamp()
                )
        if arrival_document is not None:
            write_immutable_artifact(
                run.run_dir / "arrival-profile.json",
                (run.frozen_dir / "arrival-profile.json").read_bytes(),
            )
        save(run.run_dir / "manifest.json", manifest.model_dump())
        save(run.run_dir / "trace.json", run.trace.model_dump())
        save(
            run.run_dir / "schedule.json",
            schedule,
        )
        event(
            run.run_dir,
            "schedule-frozen",
            measurementStartsAtUtc=anchor.isoformat(),
            plannedRequests=len(scheduled),
            warmupRequests=len(warm),
        )
        return TaskOutcome(value=manifest)


class ConfigureOracle(Task):
    """Configure the public API with explicitly unqualified profiles."""

    title = "Configure observed ORACLE forecasts"

    def __init__(self, run):
        """Bind the frozen observation context."""
        self.run_context = run
        mode = getattr(run, "mode", "oracle")
        if mode != "oracle":
            self.title = f"Configure observed {mode.upper()} forecasts"

    def run(self, inputs):
        """Execute this observation stage on the owned resources."""
        run = self.run_context
        manifest = CampaignManifest.model_validate_json(
            (run.run_dir / "manifest.json").read_text()
        )
        cloud = next(n.id for n in run.settings.nodes if n.kind == "cloud")
        native_settings = run.settings.model_copy(
            update={"purpose": "workflow-validation"}
        )
        with httpx.Client(trust_env=False) as http:
            for node in run.trace.nodes:
                if node == cloud:
                    continue
                client = NanoFaasOneShotClient(
                    inputs.resource(run.topology.endpoints[node]),
                    http=http,
                    timeout_seconds=operational_limit(run.settings, "api_seconds", 30),
                )
                client.load_profile(run.profile.by_node[node], revision=0)
                configuration = configuration_for_node(
                    run.topology,
                    native_settings,
                    run.profile,
                    node,
                    client=client,
                    cloud_uri=inputs.resource(run.topology.endpoints[cloud]),
                    period=manifest.period_seconds,
                    lead=manifest.lead_seconds,
                    epsilon=run.qualification.epsilon,
                    protocol=run.settings.timing.protocol,
                    anchor=datetime.fromisoformat(manifest.anchor),
                    session_id=logical_schedule(manifest)[0],
                )
                configuration["allowSynthetic"] = True
                configure_node(
                    client,
                    configuration,
                    revision=client.status().revision,
                    output=run.run_dir / "api.jsonl",
                )
                if run.mode != "oracle":
                    # Each node forecasts from the arrivals it observes; the
                    # trace stays in the run only to measure the forecast error.
                    event(
                        run.run_dir,
                        f"{run.mode}-configured",
                        node=node,
                        forecast=run.forecast,
                    )
                    continue
                receipt = client.load_trace(
                    {
                        "schemaVersion": 1,
                        "nodeId": node,
                        "revision": 1,
                        "provider": "oracle",
                        "producedAt": datetime.now(UTC).isoformat(),
                        "entries": cast(
                            JsonValue,
                            oracle_entries(
                                run.trace,
                                node=node,
                                generations=client.status().catalog_generations,
                                anchor=datetime.fromisoformat(manifest.anchor),
                            ),
                        ),
                    },
                    revision=0,
                )
                event(
                    run.run_dir,
                    "oracle-configured",
                    node=node,
                    receipt=receipt.model_dump(mode="json", by_alias=True),
                )
        return TaskOutcome()


class ObservationReport(Task):
    """Retain comparison diagnostics without rejecting measured performance."""

    title = "Report measured outcomes without performance gates"

    def __init__(self, run):
        """Bind the frozen observation context."""
        self.run_context = run

    def run(self, inputs):
        """Execute this observation stage on the owned resources."""
        run = self.run_context
        verify_files(run.frozen_dir, run.frozen)
        evidence = collect_original_evidence(run, inputs)
        responses = summarize_responses(
            json.loads((run.run_dir / "schedule.json").read_text()),
            generator_rows(run.run_dir / "generator.jsonl"),
            mode=run.mode,
        )
        save(run.run_dir / "responses.json", responses)
        record_forecast_errors(run.run_dir)
        save(
            run.run_dir / "observation.json",
            {
                "purpose": observation_purpose(run.mode),
                "qualified": False,
                "legacyComparisonValid": evidence.valid,
                "legacyDiagnostics": evidence.violations,
                "responses": responses,
                "interpretation": (
                    "historical extrapolations remain unqualified; "
                    "no performance acceptance gate"
                ),
            },
        )
        event(
            run.run_dir,
            "observation-complete",
            counts=responses["phases"]["campaign"]["total"],
        )
        return TaskOutcome(value=responses)


def retain_offline_report(directory):
    """Report already collected outcomes after compensation, also on failure."""
    if not (directory / "manifest.json").exists():
        return
    manifest = CampaignManifest.model_validate_json(
        (directory / "manifest.json").read_bytes()
    )
    if (directory / "schedule.json").exists() and not (
        directory / "responses.json"
    ).exists():
        responses = summarize_responses(
            json.loads((directory / "schedule.json").read_text()),
            generator_rows(directory / "generator.jsonl"),
            mode=manifest.mode,
        )
        save(directory / "responses.json", responses)
    if (
        all((directory / name).exists() for name in ("schedule.json", "epochs.jsonl"))
        and not (directory / "forecast-errors.csv").exists()
    ):
        record_forecast_errors(directory)
    if not (directory / "report.html").exists():
        render_report(manifest, None, directory / "report.html")
