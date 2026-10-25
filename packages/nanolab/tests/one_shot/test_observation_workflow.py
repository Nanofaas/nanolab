"""Run the package Sonata DAG and actual observation tasks against simulated I/O."""

import csv
import hashlib
import json
import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from pydantic import JsonValue
from sonata_engine import Resource

from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.one_shot import OneShotConfig, OperationalLimits
from nanolab.one_shot.supervision import CampaignSupervision
from nanolab.plans import one_shot_observation as builder
from nanolab.tasks.one_shot import (
    calibration,
    experiment,
    observation,
    preflight,
    qualification,
)
from nanolab.tasks.one_shot.artifacts import canonical_bytes
from nanolab.tasks.one_shot.trace import (
    TraceArtifact,
    oracle_entries,
    request_schedule,
    warmup_trace,
)
from tests.one_shot.test_preflight import strict_cgroup_sample


@pytest.mark.parametrize(
    ("period", "epoch_count", "profiled"),
    [(360, 9, False), (360, 9, True), (60, 54, True)],
)
@pytest.mark.parametrize(
    "failure",
    [None, "generator", "signal", "dependency", "precheck", "auction", "interrupt"],
)
@pytest.mark.parametrize("display", ["console", "tui", "json", "none"])
def test_real_nine_epoch_observation_workflow(
    tmp_path, monkeypatch, failure, profiled, period, epoch_count, display
):
    run_observation_workflow(
        tmp_path,
        monkeypatch,
        failure=failure,
        profiled=profiled,
        period=period,
        epoch_count=epoch_count,
        display=display,
    )


EWMA = {"provider": "ewma", "alpha": 0.5, "windowSeconds": 60, "maxAgeSeconds": 120}
HOLT = {
    "provider": "holt",
    "windowSeconds": 10,
    "maxAgeSeconds": 20,
    "historyWindows": 360,
    "holt": {"level": 0.4, "trend": 0.4, "damping": 0.95},
}
ARI = {
    "provider": "ari",
    "windowSeconds": 10,
    "maxAgeSeconds": 20,
    "historyWindows": 360,
    "order": 6,
}


@pytest.mark.parametrize(
    ("forecast", "failure", "properties"),
    [
        (EWMA, None, {"alpha": "0.5"}),
        (EWMA, "auction", {"alpha": "0.5"}),
        (
            HOLT,
            None,
            {
                "history": "360",
                "holt.level": "0.4",
                "holt.trend": "0.4",
                "holt.damping": "0.95",
            },
        ),
        (ARI, None, {"history": "360", "ari.order": "6"}),
    ],
)
def test_minute_observation_forecasts_from_arrivals(
    tmp_path, monkeypatch, forecast, failure, properties
):
    platforms = run_observation_workflow(
        tmp_path,
        monkeypatch,
        failure=failure,
        profiled=True,
        period=60,
        epoch_count=54,
        display="none",
        forecast=forecast,
    )
    provider = forecast["provider"]
    assert platforms["forecast_provider"] == provider.upper()
    assert platforms["forecast_properties"] == properties
    assert platforms["forecast_window_seconds"] == forecast["windowSeconds"]
    assert platforms["forecast_max_age_seconds"] == forecast["maxAgeSeconds"]
    raw = tmp_path / "raw"
    manifest = json.loads((raw / "manifest.json").read_text())
    assert (manifest["mode"], manifest["modes"]) == (provider, [provider])
    observed = json.loads((raw / "observation.json").read_text())
    assert observed["purpose"] == f"{provider}-forecast-observation"
    progress = (raw / "progress.jsonl").read_text()
    assert progress.count(f'"{provider}-configured"') == 5
    assert '"oracle-configured"' not in progress


def run_observation_workflow(
    tmp_path,
    monkeypatch,
    *,
    failure,
    profiled,
    period,
    epoch_count,
    display,
    forecast=None,
):
    raw, freeze, archive = (tmp_path / name for name in ("raw", "freeze", "archive"))
    raw.mkdir()
    freeze.mkdir()
    node_ids = [f"edge-{i}" for i in range(1, 6)] + ["cloud"]
    names = [
        "one-shot-arithmetic",
        "one-shot-sort",
        "one-shot-memory",
        "one-shot-matrix",
        "one-shot-mixed",
    ]
    payloads = [
        {"iterations": 0, "seed": 0},
        {"items": 0, "passes": 0, "seed": 0},
        {"working_set_bytes": 0, "passes": 0, "seed": 0},
        {"dimension": 0, "repetitions": 0, "seed": 0},
        {
            "arithmetic_iterations": 0,
            "sort_items": 0,
            "sort_passes": 0,
            "working_set_bytes": 0,
            "memory_passes": 0,
            "matrix_dimension": 0,
            "matrix_repetitions": 0,
            "seed": 0,
        },
    ]
    memories = [64, 128, 256, 128, 256]
    functions = {
        name: {"input": payload, "cpu": 0.25, "memoryMiB": memory, "maxReplicas": 16}
        for name, payload, memory in zip(names, payloads, memories, strict=True)
    }
    rates = {node: dict.fromkeys(names, 1 / period) for node in node_ids}
    graph = {
        "edge-1": ["edge-2", "edge-3"],
        "edge-2": ["edge-1", "edge-3"],
        "edge-3": ["edge-1", "edge-2", "edge-4", "edge-5"],
        "edge-4": ["edge-3", "edge-5"],
        "edge-5": ["edge-3", "edge-4"],
    }
    settings = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "protocolVersion": 2,
            "strictMemory": True,
            "flowQuantum": 1 / period,
            "nodes": [
                {
                    "id": node,
                    "kind": "cloud" if node == "cloud" else "edge",
                    "memoryCapacityMiB": 832,
                }
                for node in node_ids
            ],
            "functions": functions,
            "neighbors": graph,
            "operationalLimits": OperationalLimits().model_dump(by_alias=True),
            "timing": {
                "periodCandidatesSeconds": [period],
                "maxTraceResolutionSeconds": period,
                "protocol": {
                    "auctionSeconds": 60,
                    "peerSeconds": 5,
                    "solverSeconds": 5,
                    "preparationSeconds": 10,
                },
                "matrix": [{"id": "load", "rates": rates}],
            },
            "experiment": {
                "windows": [
                    {"id": f"load-{i}", "rates": rates} for i in range(epoch_count)
                ],
                "warmupSeconds": 720,
                "warmupRate": 1,
                "generatorVus": 512,
            },
        }
    )
    assert settings.timing is not None
    trace = TraceArtifact(
        nodes=node_ids,
        functions=names,
        period_seconds=period,
        flow_quantum=1 / period,
        seed=7,
        payloads=cast(
            dict[str, dict[str, JsonValue]], dict(zip(names, payloads, strict=True))
        ),
        windows=[rates] * epoch_count,
        node_roles={n: "cloud-direct" if n == "cloud" else "edge" for n in node_ids},
    )
    digest = "sha256:" + "a" * 64

    def save(name, value):
        (freeze / name).write_text(json.dumps(value))

    save("trace.json", trace.model_dump())
    save(
        "freeze.json",
        {
            "files": {},
            "source": str(tmp_path),
            "serviceScope": {},
            "hostBootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        },
    )
    digest_profile = None
    if profiled:
        cells = [
            {
                "phase": "campaign",
                "epoch": epoch,
                "origin": node,
                "function": name,
                "offsetsSeconds": [epoch * period + period / 6],
            }
            for epoch in range(epoch_count)
            for node in node_ids
            for name in names
        ]
        cells.extend(
            {
                "phase": "warmup",
                "epoch": -1,
                "origin": node,
                "function": name,
                "offsetsSeconds": [-600, -120],
            }
            for node in node_ids
            for name in names
        )
        save(
            "arrival-profile.json",
            {
                "schemaVersion": 2 if period == 60 else 1,
                "curveTimeScaleSeconds": 360,
                "periodSeconds": period,
                "epochs": epoch_count,
                "warmupSeconds": 720,
                "traceSha256": observation.sha(freeze / "trace.json"),
                "cells": cells,
            },
        )
        frozen = json.loads((freeze / "freeze.json").read_text())
        digest_profile = observation.sha(freeze / "arrival-profile.json")
        frozen["files"]["arrival-profile.json"] = digest_profile
        frozen["arrivalProfile"] = {"sha256": digest_profile}
        save("freeze.json", frozen)
    save(
        "operator-protocol.json",
        {
            "leadSeconds": 90,
            "maxOperationalFraction": 1,
            "oracleMaxAgeSeconds": 4230,
            "protocol": settings.timing.protocol.model_dump(by_alias=True),
            **({"forecast": forecast} if forecast else {}),
        },
    )
    save(
        "observations.json",
        {
            "sourcePhysicalSha256": "b" * 64,
            "measuredReplicaCountsPerFunction": dict.fromkeys(node_ids, 1),
        },
    )
    save(
        "statistics.json",
        {
            node: {
                name: {
                    "meanSeconds": 0.1,
                    "sampleCount": 10,
                    "stddevSeconds": 0.01,
                    "p95Seconds": 0.12,
                }
                for name in names
            }
            for node in node_ids
        },
    )
    acquired = []
    released = []
    now = [100.0]
    states = {}
    calls = []
    wall = datetime(2026, 10, 13, tzinfo=UTC).timestamp()

    class VirtualDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(wall + now[0], tz or UTC)

    signalled = []

    def sleep(seconds):
        now[0] += seconds
        if (
            failure in ("signal", "interrupt")
            and not signalled
            and (raw / "manifest.json").exists()
        ):
            anchor = json.loads((raw / "manifest.json").read_text())["parameters"][
                "logicalSchedule"
            ]["anchorMonotonicSeconds"]
            if now[0] >= anchor + 720:
                signalled.append(True)
                os.kill(
                    os.getpid(),
                    signal.SIGINT if failure == "interrupt" else signal.SIGTERM,
                )

    clock = SimpleNamespace(
        monotonic=lambda: now[0], sleep=sleep, time=lambda: wall + now[0]
    )
    for module in (observation, qualification, experiment, calibration, preflight):
        monkeypatch.setattr(module, "time", clock)
        if hasattr(module, "datetime"):
            monkeypatch.setattr(module, "datetime", VirtualDate)

    def resource(name, value, requires=()):
        def acquire(inputs):
            for dependency in requires:
                inputs.resource(dependency)
            acquired.append(name)
            return value

        return Resource(
            title=name,
            acquire=acquire,
            release=lambda _inputs, _value: released.append(name),
            requires=requires,
        )

    vms = {
        node: resource(
            "VM " + node,
            SimpleNamespace(name=node, host=node, home="/home/ubuntu", user="ubuntu"),
        )
        for node in node_ids
    }
    nodes = {
        n: SimpleNamespace(
            vm=vms[n],
            config=next(node for node in settings.nodes if node.id == n),
            request=SimpleNamespace(name=n),
            command=lambda *a, **k: SimpleNamespace(
                stdout='{"bytes":8388608,"seconds":0.1}'
            ),
        )
        for n in node_ids
    }
    endpoints = {
        n: resource("control-plane " + n, "http://" + n + ":8080", (vms[n],))
        for n in node_ids
    }
    probes = {
        n: resource("probe " + n, "http://" + n + ":17111", (vms[n],)) for n in node_ids
    }
    distribution = resource(
        "distribution",
        SimpleNamespace(
            source={"revision": "a" * 40},
            components=[
                SimpleNamespace(
                    kind="function", name=n, image=SimpleNamespace(id=digest)
                )
                for n in names
            ],
        ),
    )
    topology = SimpleNamespace(
        resources=SimpleNamespace(
            nodes=nodes, host_cpu_placement=None, generator_location="operator-host"
        ),
        endpoints=endpoints,
        probes=probes,
        distribution=distribution,
        collectors={},
        neighbor_graph=graph,
        function_settings=settings.functions,
        function_aliases={n: n for n in names},
        node_function_settings=dict.fromkeys(node_ids, settings.functions),
        requires=(*vms.values(), *endpoints.values(), *probes.values(), distribution),
    )
    platforms = {}
    monkeypatch.setattr(
        builder,
        "add_one_shot_platforms",
        lambda *a, **k: platforms.update(k) or topology,
    )
    monitor = SimpleNamespace(refresh=lambda: None, require_healthy=lambda: None)
    monkeypatch.setattr(
        builder,
        "clock_health_resource",
        lambda **k: resource("clock", monitor, tuple(probes.values())),
    )

    def container(node, function, **_):
        fn = settings.functions[function]
        return "http://" + node.config.id + ":1234/" + function, {
            "Image": digest,
            "HostConfig": {
                "Memory": fn.memory_mib * 1024 * 1024,
                "MemorySwap": fn.memory_mib * 1024 * 1024,
                "NanoCpus": 250000000,
            },
            "function": function,
        }

    def cgroup(node, obj):
        return {
            **strict_cgroup_sample(),
            "memory.max": str(
                settings.functions[obj["function"]].memory_mib * 1024 * 1024
            ),
        }

    for module in (preflight, observation, experiment):
        monkeypatch.setattr(module, "runtime_endpoint", container)
    monkeypatch.setattr(preflight, "collect_container_cgroup", cgroup)
    monkeypatch.setattr(observation, "collect_container_cgroup", cgroup)
    monkeypatch.setattr(experiment, "runtime_inventory", lambda *a, **k: [])
    original_http = httpx.Client

    def respond(request):
        path = request.url.path
        node = request.url.host
        data = json.loads(request.content) if request.content else {}
        calls.append((node, path, data))
        if path == "/inventory":
            result = {
                "cpus": 2,
                "memoryMiB": 4096,
                "os": "simulated",
                "architecture": "aarch64",
            }
        elif path == "/clock":
            result = {
                "timestamp": wall + now[0],
                "monotonicSeconds": now[0],
                "bootId": node + "-boot",
            }
        elif path == "/v1/functions":
            result = [{"name": name} for name in names]
        elif path.endswith("/runtime/status"):
            result = {
                "schemaVersion": 1,
                "physicalReleaseProof": True,
                "maxConcurrentHandlers": 1,
            }
        elif path.endswith("/invoke"):
            fn = path.split("/")[1]
            result = calibration.workload_checksum(data["input"], function=fn)
        elif "/runtime/executions/" in path:
            assert request.extensions["timeout"]["read"] == 120
            result = {
                "executionId": path.split("/runtime/executions/")[1],
                "incarnation": "runtime",
                "state": "RELEASED",
                "handlerStarted": True,
                "dispatchAttempt": "1",
                "occupancySeconds": 0.1,
            }
        elif path.startswith("/v1/functions/"):
            result = {
                "runtimeMode": "HTTP",
                "scalingConfig": {
                    "strategy": "NONE",
                    "concurrencyControl": {"targetInFlightPerPod": 1},
                },
            }
        elif "/profiles/" in path:
            result = {
                "revision": 1,
                "contentHash": "sha256:" + hashlib.sha256(request.content).hexdigest(),
                "profile": data,
                "bytes": len(request.content),
            }
        elif path.endswith("/config"):
            states[node] = data
            result = {"revision": 1, "settings": data}
        elif path.endswith("/forecasting/trace"):
            result = {
                "revision": 1,
                "entryCount": len(data["entries"]),
                "nodeId": node,
                "producedAt": data["producedAt"],
            }
        elif path.endswith("/prometheus") or path.endswith("/metrics"):
            return httpx.Response(
                503
            )  # Missing diagnostic metrics do not stop usable plans.
        elif path.endswith("/events"):
            return httpx.Response(503)
        elif path.endswith("/prepare") or path.endswith("/activate"):
            if (
                failure == "auction"
                and path.endswith("/prepare")
                and node == "edge-2"
                and int(path.split("/")[-2]) == 2
            ):
                return httpx.Response(
                    200,
                    json={
                        "status": "FAILED",
                        "plan": None,
                        "desiredReplicas": {},
                        "readyReplicas": {},
                        "reason": "auction deadline",
                        "preparedAt": VirtualDate.now(UTC).isoformat(),
                    },
                )
            epoch = int(path.split("/")[-2])
            preparing = path.endswith("/prepare")
            plan = {
                "nodeId": node,
                "flowQuantum": 1 / period,
                "functions": {
                    fn: {
                        "readyReplicas": 1,
                        "memoryMiB": settings.functions[fn].memory_mib,
                        "utilization": 0.8,
                        "demandSeconds": 0.1,
                        "forecastRate": 1 / period,
                        "localRate": 0,
                        "cloudRate": 1 / period,
                        "inbound": [],
                        "outbound": [],
                    }
                    for fn in names
                },
            }
            result = {
                "status": "NEGOTIATED" if preparing else "DEGRADED",
                "plan": None if preparing else plan,
                "desiredReplicas": dict.fromkeys(names, 1),
                "readyReplicas": dict.fromkeys(names, 1),
                "reason": "simulated incomplete confirmations",
                "preparedAt": VirtualDate.now(UTC).isoformat(),
                "diagnostics": [
                    {
                        "observedAtUtc": "2026-10-13T00:00:00Z",
                        "monotonicOffsetSeconds": 1,
                        "sessionId": "test",
                        "nodeId": node,
                        "epoch": epoch,
                        "round": 7,
                        "assignmentId": f"{epoch}/grant",
                        "function": names[0],
                        "version": "1",
                        "buyerGeneration": 1,
                        "sellerGeneration": 1,
                        "buyerId": "edge-1",
                        "buyerIncarnation": "buyer",
                        "sellerId": "edge-2",
                        "sellerIncarnation": "seller",
                        "negotiatedQuantity": 1,
                        "confirmedQuantity": 0,
                        "readyReceived": True,
                        "ackSent": True,
                        "ackReceived": False,
                        "ackResponseReceived": False,
                        "reason": "TRANSPORT_ERROR",
                        "residualCloudRate": 1 / period,
                    }
                ]
                if not preparing and node in {"edge-1", "edge-2"}
                else [],
            }
        elif path.endswith("/p2p/peers"):
            result = [
                {"id": peer, "active": True, "mode": "FORCE_ACTIVE"}
                for peer in graph.get(node, [])
            ]
        elif path.endswith("/status"):
            result = {
                "schemaVersion": 2,
                "sessionId": states.get(node, {}).get("sessionId", "preflight"),
                "state": "IDLE",
                "busy": False,
                "clockHealthy": False,
                "catalogGenerations": dict.fromkeys(names, 1),
                "peerEndpoints": [
                    {
                        "peerId": peer,
                        "incarnation": peer + "-run",
                        "invocationUri": "http://" + peer + ":8080",
                    }
                    for peer in graph.get(node, [])
                ],
                "localEndpoint": {},
                "revision": 1 if node in states else 0,
                "activePlan": {},
            }
        else:
            raise AssertionError((node, path))
        return httpx.Response(200, json=result)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_http(transport=httpx.MockTransport(respond)),
    )

    class Sampler:
        def __init__(self, run, inputs, **kwargs):
            self.run = run
            self.sessions = []
            self.closed = False
            for vm in vms.values():
                inputs.resource(vm)

        def require_healthy(self):
            pass

        def close(self):
            if self.closed:
                return
            self.closed = True
            directory = self.run.run_dir / "vm-resources"
            directory.mkdir(exist_ok=True)
            for node in node_ids:
                (directory / (node + ".jsonl")).write_text(
                    json.dumps(
                        {
                            "timestampUtc": VirtualDate.now(UTC).isoformat(),
                            "node": node,
                            "monotonicSeconds": now[0],
                            "bootId": node + "-boot",
                            "cpuUtilization": 0.2,
                            "vcpuCount": 2,
                            "memoryTotalBytes": 4096,
                            "memoryAvailableBytes": 2048,
                            "memoryUsedBytes": 2048,
                            "swapTotalBytes": 0,
                            "swapFreeBytes": 0,
                            "swapUsedBytes": 0,
                            "containers": [],
                        }
                    )
                    + "\n"
                )

    monkeypatch.setattr(observation, "VmTelemetrySession", Sampler)

    def telemetry(run, *, clock):
        return Resource(
            title="sampler",
            acquire=lambda inputs: Sampler(run, inputs),
            release=lambda _, s: s.close(),
            requires=() if failure == "dependency" else (*topology.requires, clock),
        )

    monkeypatch.setattr(builder, "vm_telemetry_resource", telemetry)
    if failure == "precheck":

        def failed_precheck(self, inputs):
            raise RuntimeError("injected observer precheck failure")

        monkeypatch.setattr(observation.ObserverPrecheck, "run", failed_precheck)
    collector = SimpleNamespace(require_healthy=lambda: None, close=lambda: None)
    monkeypatch.setattr(
        builder,
        "evidence_collector_resource",
        lambda run, generator: resource(
            "collector", collector, (*topology.requires, generator)
        ),
    )

    class Child:
        returncode = None

        def __init__(self, *args, **kwargs):
            self.manifest = json.loads((raw / "manifest.json").read_text())
            self.anchor = self.manifest["parameters"]["logicalSchedule"][
                "anchorMonotonicSeconds"
            ]
            # A deliberate generation deficit remains an observation.
            schedule = json.loads((raw / "schedule.json").read_text())
            for row in schedule[:1]:
                for kind in ("emitted", "result"):
                    kwargs["stdout"].write(
                        json.dumps(
                            dict(
                                row,
                                event=kind,
                                emittedAt=row["scheduledAt"],
                                finishedAt=row["scheduledAt"] + 0.1,
                                statusCode=429,
                                responseStatus=None,
                                responseBody="queue full",
                                outputJson=None,
                                latencySeconds=0.1,
                            )
                        )
                        + "\n"
                    )
            kwargs["stdout"].flush()

        def poll(self):
            if failure == "generator" and now[0] >= self.anchor + 720:
                self.returncode = 7
            elif now[0] >= self.anchor + epoch_count * period:
                self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, **kwargs):
            return self.returncode

    monkeypatch.setattr(experiment.subprocess, "Popen", Child)
    from io import StringIO

    from rich.console import Console

    from nanolab.one_shot.views import open_presentation, select_presentation

    def presentation(fail):
        if display == "tui":
            return open_presentation(
                "tui", console=Console(file=StringIO()), on_failure=fail
            )
        return select_presentation(display, on_failure=fail)

    supervisor = CampaignSupervision(
        raw, archive=archive, cadence=100, presentation_factory=presentation
    )
    workflow = builder.build_one_shot_observation_plan(
        ScenarioConfig.model_construct(one_shot=settings),
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
        ),
        freeze_dir=freeze,
        run_dir=raw,
        supervision=supervisor,
    )
    assert not acquired
    if failure not in (None, "auction"):
        with pytest.raises(
            Exception, match=r"generator|signal 15|signal 2|resource|precheck"
        ):
            supervisor.run(workflow.run)
    else:
        supervisor.run(workflow.run)
    if profiled:
        assert (raw / "arrival-profile.json").read_bytes() == (
            freeze / "arrival-profile.json"
        ).read_bytes()
    manifest: dict = {}
    if failure != "precheck":
        manifest = json.loads((raw / "manifest.json").read_text())
        schedule = json.loads((raw / "schedule.json").read_text())
        if profiled:
            assert (raw / "arrival-profile.json").read_bytes() == (
                freeze / "arrival-profile.json"
            ).read_bytes()
            assert (
                manifest["parameters"]["generator"]["arrivalProfile"]["sha256"]
                == digest_profile
            )
            assert len(schedule) == (epoch_count + 2) * 30
            anchor = datetime.fromisoformat(manifest["anchor"]).timestamp()
            assert all(
                row["scheduledAt"] == anchor + row["epoch"] * period + period / 6
                for row in schedule
                if row["phase"] == "campaign"
            )
            assert {
                row["scheduledAt"] - anchor
                for row in schedule
                if row["phase"] == "warmup"
            } == {-600, -120}
        else:
            assert not (raw / "arrival-profile.json").exists()
            assert "arrivalProfile" not in manifest["parameters"]["generator"]
            anchor_utc = datetime.fromisoformat(manifest["anchor"])
            endpoints = {
                row["origin"]: row["url"].split("/v1/functions/")[0] for row in schedule
            }
            baseline = request_schedule(
                trace, anchor=anchor_utc, endpoints=endpoints, run_id=manifest["run_id"]
            )
            warm = request_schedule(
                warmup_trace(trace, seconds=720),
                anchor=anchor_utc - timedelta(seconds=720),
                endpoints=endpoints,
                run_id=manifest["run_id"] + ":warmup",
            )
            baseline.extend(dict(row, phase="warmup", epoch=-1) for row in warm)
            baseline.sort(key=lambda row: (row["scheduledAt"], row["originalId"]))
            assert (raw / "schedule.json").read_bytes() == canonical_bytes(baseline)
    assert sorted(acquired) == sorted(released)
    assert len(released) == len(set(released))
    assert (archive / "result.json").exists()
    result = json.loads((archive / "result.json").read_text())
    assert result["exitCode"] == (
        0
        if failure in (None, "auction")
        else 143
        if failure == "signal"
        else 130
        if failure == "interrupt"
        else 1
    )
    if failure in (None, "auction"):
        epochs = [
            json.loads(line) for line in (raw / "epochs.jsonl").read_text().splitlines()
        ]
        assert [row["epoch"] for row in epochs] == list(range(epoch_count))
        assert sum(row["censored"] for row in epochs) == (
            1 if failure == "auction" else 0
        )
        assert all(not row["measurementComplete"] for row in epochs)
        configs = [data for _, path, data in calls if path.endswith("/config")]
        assert len(configs) == 5
        uploads = [
            (node, data)
            for node, path, data in calls
            if path.endswith("/forecasting/trace")
        ]
        assert len(uploads) == (0 if forecast else 5)
        errors = list(csv.DictReader((raw / "forecast-errors.csv").open()))
        assert len(errors) == epoch_count * 25
        planned = [row for row in errors if row["forecastSource"] == "plan"]
        assert len(errors) - len(planned) == (5 if failure == "auction" else 0)
        # The simulated generator emits only one warmup request.
        assert all(
            float(row["error"]) == float(row["predictedRate"]) == 1 / period
            for row in planned
        )
        for node, data in uploads:
            assert data["entries"] == oracle_entries(
                trace,
                node=node,
                generations=dict.fromkeys(names, 1),
                anchor=datetime.fromisoformat(manifest["anchor"]),
            )
        assert len(
            [path for _, path, _ in calls if path.endswith("/activate")]
        ) == epoch_count * 5 - (1 if failure == "auction" else 0)
        assert all(
            config["leadTime"] == "PT90.000000000S"
            and config["preparationBudget"] == "PT10.000000000S"
            for config in configs
        )
        assert all(config["negotiation"]["maxRounds"] == 8 for config in configs)
        assert all(config["period"] == f"PT{period:.9f}S" for config in configs)
        assert manifest["period_seconds"] == period
        report = json.loads((raw / "report.json").read_text())
        cells = report["assignmentObservations"]["cells"]
        missing_ack = [cell for cell in cells if cell["diagnostics"]]
        assert len(missing_ack) == epoch_count * 2 - (1 if failure == "auction" else 0)
        assert all(cell["unconfirmedQuantity"] == 1 for cell in missing_ack)
        assert all(
            cell["diagnostics"][0]["reason"] == "TRANSPORT_ERROR"
            for cell in missing_ack
        )
        assert report["operationalOutcome"]["state"] == "completed"
        assert (
            json.loads((raw / "observation.json").read_text())["legacyComparisonValid"]
            is False
        )
    for path in raw.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (archive / path.relative_to(raw)).read_bytes()
    return platforms
