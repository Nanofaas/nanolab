"""Reject violations of the physical experiment assumptions before load."""

from typing import Any, cast

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
    monotonic_stamps = iter([100.0, 100.002, 200.0, 200.02])
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.time",
        SimpleNamespace(
            time=lambda: next(stamps), monotonic=lambda: next(monotonic_stamps)
        ),
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"timestamp": 100.001})
        )
    ) as http:
        offset, uncertainty, measured_at = measure_http_clock(
            "http://node:17111", http=http, repetitions=2
        )
    assert abs(offset) < 1e-8
    assert uncertainty == pytest.approx(0.001)
    assert measured_at.timestamp() == 100.001


@pytest.mark.parametrize(
    "failure",
    [
        "release",
        "image",
        "resources",
        "mode",
        "scaling",
        "network",
        "cpu",
        "peer",
        "swap",
        "cgroup",
        "strict-healthy",
        "clock-jump",
        None,
    ],
)
def test_collection_validates_measured_facts_and_preserves_failures(
    tmp_path, monkeypatch, failure
):
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
    values.update(
        {resource: "http://" + key + ":8080" for key, resource in endpoints.items()}
    )
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
        lambda *args, **kwargs: SimpleNamespace(
            stdout='{"bytes": 1, "seconds": 0.1}'
            if failure == "network"
            else '{"bytes": 8388608, "seconds": 0.1}'
        ),
    )

    def respond(request):
        if request.url.path == "/v1/functions":
            return httpx.Response(200, json=[{"name": "f"}])
        if request.url.path == "/inventory":
            return httpx.Response(
                200,
                json={
                    "cpus": 1 if failure == "cpu" else 2,
                    "memoryMiB": 3900,
                    "os": "fake-test",
                    "architecture": "fake-test",
                },
            )
        if request.url.path == "/runtime/status":
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 1,
                    "physicalReleaseProof": failure != "release",
                    "maxConcurrentHandlers": 1,
                },
            )
        if request.url.path == "/v1/functions/f":
            return httpx.Response(
                200,
                json={
                    "runtimeMode": None if failure == "mode" else "HTTP",
                    "scalingConfig": {
                        "strategy": "HPA" if failure == "scaling" else "NONE",
                        "concurrencyControl": {"targetInFlightPerPod": 1},
                    },
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
                        "peerId": "edge-99"
                        if failure == "peer"
                        else ("edge-1" if request.url.host == "edge-0" else "edge-0"),
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

    def clock_measurement(*args, **kwargs):
        kwargs["record_probe"](
            {
                "remoteUtcSeconds": 100.001,
                "errorType": None,
                "rttSeconds": 0.002,
                "hostUtcJumpSeconds": 18 if failure == "clock-jump" else 0,
            }
        )
        return 0.001, 9.001 if failure == "clock-jump" else 0.001, datetime.now(UTC)

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.measure_http_clock", clock_measurement
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.runtime_endpoint",
        lambda *args, **kwargs: (
            "http://runtime:8080",
            {
                "Image": "sha256:" + ("b" if failure == "image" else "a") * 64,
                "HostConfig": {
                    "Memory": (256 if failure == "resources" else 128) * 1024 * 1024,
                    "NanoCpus": 1000000000,
                    "MemorySwap": 1 if failure == "swap" else 128 * 1024 * 1024,
                },
            },
        ),
    )

    def cgroups(*args):
        if failure == "cgroup":
            raise ValueError("remote cgroup evidence unavailable")
        sample = strict_cgroup_sample()
        sample["memory.max"] = str(128 * 1024 * 1024)
        sample["cpu.max"] = "100000 100000"
        return sample

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.collect_container_cgroup", cgroups
    )
    output = tmp_path / "preflight.json"
    task = CollectTopologyTask(
        resources,
        endpoints=endpoints,
        probes=probes,
        distribution=distribution,
        function_settings={"f": settings.functions["f"]},
        output=output,
        strict_memory=failure in {"swap", "cgroup", "strict-healthy"},
        protocol_version=2 if failure == "clock-jump" else 1,
    )
    inputs = TaskInputs._for_resources(values, set(values))
    import json

    if failure not in {None, "strict-healthy", "clock-jump"}:
        reasons = {
            "release": "physical concurrency",
            "image": "digest",
            "resources": "resource mismatch",
            "mode": "runtime identity",
            "scaling": "HPA/scaling",
            "network": "network transfer",
            "cpu": "CPU allocation",
            "peer": "neighbor",
            "swap": "memory/swap",
            "cgroup": "cgroup",
        }
        with pytest.raises(ValueError, match=reasons[failure]):
            task.run(inputs)
        assert json.loads(output.read_text())["status"] != "PASS"
    else:
        result = task.run(inputs).value
        assert result is not None and len(result.nodes) == 3
        assert all(node.bandwidth_mbps > 0 for node in result.nodes)
        assert all(node.rtt_seconds == pytest.approx(0.002) for node in result.nodes)
        if failure == "strict-healthy":
            assert all(node.runtime_limits for node in result.nodes)
    assert output.is_file()


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


def test_preflight_task_requires_all_declared_nodes_under_resource_ownership():
    from types import SimpleNamespace

    from sonata_engine import Resource, TaskInputs

    from nanolab.tasks.one_shot.preflight import PreflightTask

    nodes = {
        name: SimpleNamespace(
            vm=Resource(title=name, acquire=lambda _: None, release=lambda *_: None)
        )
        for name in ["edge-0", "edge-1", "cloud"]
    }
    rows = [
        evidence(node_id=name, kind="cloud" if name == "cloud" else "edge")
        for name in nodes
    ]
    resources = cast(
        Any, SimpleNamespace(nodes=nodes, generator_location="operator-host")
    )
    values = {node.vm: None for node in nodes.values()}
    inputs = TaskInputs._for_resources(values, set(values))
    task = PreflightTask(resources, evidence=rows, max_clock_skew_seconds=0.1)
    outcome = task.run(inputs).value
    assert outcome is not None and len(outcome.nodes) == 3
    task.evidence = rows[:2]
    with pytest.raises(ValueError, match="incomplete topology"):
        task.run(inputs)


def test_ssh_clock_preserves_selected_remote_timestamp_and_actual_rtt(monkeypatch):
    from types import SimpleNamespace

    from nanolab.tasks.one_shot.preflight import measure_clock

    times = iter([100.0, 100.002, 200.0, 200.04])
    remote = iter(["100.001", "200.02"])
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.time",
        SimpleNamespace(time=lambda: next(times)),
    )
    node = cast(
        Any, SimpleNamespace(command=lambda _: SimpleNamespace(stdout=next(remote)))
    )
    offset, uncertainty, stamp = measure_clock(node, repetitions=2)
    assert offset == pytest.approx(0)
    assert uncertainty == pytest.approx(0.001)
    assert stamp.timestamp() == 100.001


def strict_cgroup_sample():
    return {
        "containerId": "a" * 64,
        "pid": 123,
        "cgroup": "/system.slice/docker-" + "a" * 64 + ".scope",
        "memory.max": str(64 * 1024 * 1024),
        "memory.swap.max": "0",
        "memory.current": "1048576",
        "memory.peak": "2097152",
        "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
        "cpu.max": "25000 100000",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory.max", "64"),
        ("memory.max", "max"),
        ("memory.swap.max", "67108864"),
        ("cpu.max", "max 100000"),
        ("cpu.max", "50000 100000"),
        ("memory.events", "oom 1\noom_kill 1"),
        ("memory.peak", None),
    ],
)
def test_strict_limits_reject_missing_or_incorrect_physical_evidence(field, value):
    from nanolab.tasks.one_shot.preflight import validate_strict_cgroup

    sample = strict_cgroup_sample()
    if value is None:
        del sample[field]
    else:
        sample[field] = value
    with pytest.raises(ValueError, match="cgroup"):
        validate_strict_cgroup(sample, memory_mib=64, cpu=0.25)


def test_strict_limits_accept_actual_bytes_zero_swap_and_cpu_quota():
    from nanolab.tasks.one_shot.preflight import validate_strict_cgroup

    validate_strict_cgroup(strict_cgroup_sample(), memory_mib=64, cpu=0.25)


def test_cgroup_remote_failure_is_preserved():
    from types import SimpleNamespace

    from nanolab.one_shot.infrastructure import NodeResource
    from nanolab.tasks.one_shot.preflight import collect_container_cgroup

    def fail(*args, **kwargs):
        raise RuntimeError("remote cgroup unavailable")

    with pytest.raises(RuntimeError, match="remote cgroup unavailable"):
        collect_container_cgroup(
            cast(NodeResource, SimpleNamespace(command=fail)),
            {"Id": "a" * 64, "State": {"Pid": 123, "Running": True}},
        )


def test_clock_probe_preserves_each_attempt_before_selecting_minimum(monkeypatch):
    """Dropping a slow probe would hide the uncertainty that needs diagnosis."""
    from types import SimpleNamespace

    import httpx

    from nanolab.tasks.one_shot.preflight import measure_http_clock

    stamps = iter([100.0, 100.2, 200.0, 200.002])
    monotonic_stamps = iter([100.0, 100.2, 200.0, 200.002])
    remotes = iter([100.1, 200.001])
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.time",
        SimpleNamespace(
            time=lambda: next(stamps), monotonic=lambda: next(monotonic_stamps)
        ),
    )
    attempts = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"timestamp": next(remotes)})
        )
    ) as http:
        offset, uncertainty, at = measure_http_clock(
            "http://node", http=http, repetitions=2, record_probe=attempts.append
        )
    assert len(attempts) == 2
    assert attempts[0]["beforeUtcSeconds"] == 100.0
    assert attempts[0]["afterUtcSeconds"] == 100.2
    assert attempts[0]["remoteUtcSeconds"] == 100.1
    assert attempts[0]["rttSeconds"] == pytest.approx(0.2)
    assert attempts[1]["attempt"] == 1
    assert uncertainty == pytest.approx(0.001)
    assert offset == pytest.approx(0)
    assert at.timestamp() == 200.001


def test_clock_probe_preserves_failed_attempt_and_raises(monkeypatch):
    """A failed request must leave evidence without becoming an accepted sample."""
    from types import SimpleNamespace

    import httpx

    from nanolab.tasks.one_shot.preflight import measure_http_clock

    stamps = iter([100.0, 101.0])
    monotonic_stamps = iter([100.0, 101.0])
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.time",
        SimpleNamespace(
            time=lambda: next(stamps), monotonic=lambda: next(monotonic_stamps)
        ),
    )
    attempts = []

    def fail(request):
        raise httpx.ReadTimeout("probe timed out", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(fail)) as http,
        pytest.raises(httpx.ReadTimeout),
    ):
        measure_http_clock(
            "http://node", http=http, repetitions=3, record_probe=attempts.append
        )
    assert len(attempts) == 1
    assert attempts[0]["beforeUtcSeconds"] == 100.0
    assert attempts[0]["afterUtcSeconds"] == 101.0
    assert attempts[0]["errorType"] == "ReadTimeout"
    assert attempts[0]["remoteUtcSeconds"] is None
