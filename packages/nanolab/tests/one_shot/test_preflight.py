"""Reject violations of the physical experiment assumptions before load."""

from typing import Any

import pytest

from nanolab.tasks.one_shot.preflight import NodeEvidence, validate_node


def evidence(**changes):
    data = {
        "node_id": "edge-0",
        "kind": "edge",
        "vm_name": "run-edge-0",
        "host": "10.0.0.1",
        "cpus": 2,
        "memory_mib": 4096,
        "memory_capacity_mib": 512,
        "backend": "container-local",
        "concurrency": 1,
        "hpa_active": False,
        "ready": True,
        "peer_count": 1,
        "image_digests": {"f": "sha256:" + "a" * 64},
        "clock_offset_seconds": 0.001,
        "clock_uncertainty_seconds": 0.001,
        "rtt_seconds": 0.001,
        "bandwidth_mbps": 100,
    }
    data.update(changes)
    return NodeEvidence.model_validate(data)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"kind": "cloud", "ready": False}, "ready"),
        ({"hpa_active": True}, "HPA"),
        ({"memory_capacity_mib": 5000}, "memory"),
        ({"clock_offset_seconds": 1.0}, "clock"),
        ({"concurrency": 2}, "concurrency"),
        ({"peer_count": 0}, "peer"),
    ],
)
def test_preflight_rejects_invalid_assumptions(changes, reason):
    with pytest.raises(ValueError, match=reason):
        validate_node(evidence(**changes), max_clock_skew_seconds=0.1)


def test_preflight_accepts_measured_local_node():
    validate_node(evidence(), max_clock_skew_seconds=0.1)


def test_http_clock_uses_server_timestamp_and_measured_uncertainty(monkeypatch):
    from types import SimpleNamespace

    import httpx

    from nanolab.tasks.one_shot.preflight import measure_http_clock

    stamps = iter([100.0, 100.002, 200.0, 200.02])
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.time",
        SimpleNamespace(time=lambda: next(stamps)),
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"timestamp": 100.001})
        )
    ) as http:
        offset, uncertainty, _ = measure_http_clock(
            "http://node:17111", http=http, repetitions=2
        )
    assert abs(offset) < 1e-8
    assert uncertainty == pytest.approx(0.001)


def test_failed_collection_preserves_partial_evidence(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    import httpx
    from sonata_engine import Resource, TaskInputs

    from nanolab.config import EnvironmentConfig
    from nanolab.config.one_shot import OneShotConfig
    from nanolab.one_shot.infrastructure import build_one_shot_resources
    from nanolab.tasks.one_shot.preflight import CollectTopologyTask
    from nanolab.tasks.vm.models import VmInfo

    settings = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "nodes": [
                {"id": "edge-0", "kind": "edge"},
                {"id": "edge-1", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "functions": {"f": {"input": {}}},
        }
    )
    resources = build_one_shot_resources(
        settings,
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
        ),
        run_id="fake",
        repo_root=tmp_path,
    )

    def resource(title):
        return Resource(title=title, acquire=lambda _: None, release=lambda *args: None)

    endpoints = {key: resource(key + "endpoint") for key in resources.nodes}
    probes = {key: resource(key + "probe") for key in resources.nodes}
    distribution = resource("distribution")
    values: dict[Resource[Any], Any] = {
        node.vm: VmInfo(
            name=node.request.name or "missing",
            host="10.0.0.1",
            user="ubuntu",
            home="/home/ubuntu",
        )
        for node in resources.nodes.values()
    }
    values.update(dict.fromkeys(endpoints.values(), "http://node:8080"))
    values.update(dict.fromkeys(probes.values(), "http://probe:17111"))
    values[distribution] = SimpleNamespace(
        components=[
            SimpleNamespace(
                kind="function",
                name="f",
                image=SimpleNamespace(id="sha256:" + "a" * 64),
            )
        ]
    )

    monkeypatch.setattr(
        "nanolab.one_shot.infrastructure.NodeResource.command",
        lambda *args, **kwargs: SimpleNamespace(stdout=""),
    )

    def respond(request):
        if request.url.path == "/v1/functions":
            return httpx.Response(200, json=[{"name": "f"}])
        if request.url.path == "/inventory":
            return httpx.Response(200, json={"cpus": 2, "memoryMiB": 3900})
        if request.url.path == "/runtime/status":
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 1,
                    "physicalReleaseProof": False,
                    "maxConcurrentHandlers": 1,
                },
            )
        if request.url.path == "/v1/admin/offload/one-shot/clock-health":
            return httpx.Response(200, json={"healthy": True})
        return httpx.Response(
            200,
            json={
                "schemaVersion": 1,
                "state": "IDLE",
                "busy": False,
                "clockHealthy": True,
                "catalogGenerations": {"f": 1},
                "peerEndpoints": [
                    {
                        "peerId": "edge-1",
                        "incarnation": "i",
                        "invocationUri": "http://other:8080",
                    }
                ],
                "localEndpoint": {},
                "revision": 0,
                "activePlan": {},
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.httpx.Client", lambda **kwargs: http
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.measure_http_clock",
        lambda *args, **kwargs: (0.001, 0.001, datetime.now(UTC)),
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.runtime_endpoint",
        lambda *args, **kwargs: ("http://runtime:8080", {}),
    )
    output = tmp_path / "preflight.json"
    task = CollectTopologyTask(
        resources,
        endpoints=endpoints,
        probes=probes,
        distribution=distribution,
        function_settings={"f": settings.functions["f"]},
        output=output,
    )
    inputs = TaskInputs._for_resources(values, set(values))
    with pytest.raises(ValueError, match="physical concurrency"):
        task.run(inputs)
    assert output.is_file()
    import json

    assert json.loads(output.read_text())["status"] != "PASS"


def test_clock_monitor_surfaces_rejected_refresh_and_preserves_samples(tmp_path):
    from nanolab.tasks.one_shot.clock import ClockMonitor

    attempts = []

    def refresh():
        attempts.append(True)
        raise ValueError("measured clock unhealthy")

    monitor = ClockMonitor(refresh, interval_seconds=1)
    with pytest.raises(ValueError, match="clock unhealthy"):
        monitor.start()
    assert len(attempts) == 1
    monitor.close()


def test_background_clock_failure_invalidates_consumer():
    from threading import Event

    from nanolab.tasks.one_shot.clock import ClockMonitor

    refreshed = Event()
    attempts = []

    def refresh():
        attempts.append(True)
        if len(attempts) > 1:
            refreshed.set()
            raise ValueError("rejected update")

    monitor = ClockMonitor(refresh, interval_seconds=0.01)
    monitor.start()
    assert refreshed.wait(1)
    monitor.close()
    with pytest.raises(RuntimeError, match="monitor failed"):
        monitor.require_healthy()
    assert len(attempts) == 2


def test_clock_resource_bounds_api_time_and_retains_real_measurement(
    tmp_path, monkeypatch
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from sonata_engine import Resource, TaskInputs

    from nanolab.tasks.one_shot.clock import clock_health_resource

    endpoint = Resource(title="edge", acquire=lambda _: "", release=lambda *_: None)
    probe = Resource(title="probe", acquire=lambda _: "", release=lambda *_: None)
    seen = []

    def client(url, *, http, timeout_seconds):
        seen.append(timeout_seconds)
        return SimpleNamespace(
            update_clock_health=lambda **kw: SimpleNamespace(healthy=True)
        )

    monkeypatch.setattr("nanolab.tasks.one_shot.clock.NanoFaasOneShotClient", client)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.clock.measure_http_clock",
        lambda *args, **kw: (0.002, 0.001, datetime.now(UTC)),
    )
    owned = clock_health_resource(
        endpoints={"edge": endpoint},
        probes={"edge": probe},
        output=tmp_path / "clock.jsonl",
    )
    values = {endpoint: "http://edge", probe: "http://probe"}
    inputs = TaskInputs._for_resources(values, set(values))
    monitor = owned.acquire(inputs)
    monitor.close()
    assert seen == [2]
    assert '"offsetSeconds": 0.002' in (tmp_path / "clock.jsonl").read_text()
