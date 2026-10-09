from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from nanolab.config.one_shot import OneShotConfig
from nanolab.tasks.one_shot.calibration import (
    MeasureServiceTask,
    PublishCalibrationTask,
    ValidateCapacityTask,
    WarmupTask,
)


@pytest.mark.parametrize("capacity_drift", [False, True])
def test_independent_calibration_preserves_raw_and_refuses_capacity_drift(
    tmp_path, monkeypatch, capacity_drift
):
    settings = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "nodes": [
                {"id": "edge-0", "kind": "edge"},
                {"id": "edge-1", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "functions": {
                "one-shot-workload": {
                    "input": {"iterations": 0, "working_set_bytes": 0, "seed": 7}
                }
            },
            "calibration": {
                "minSamples": 2,
                "maxSamples": 2,
                "repetitions": 2,
                "capacitySeconds": 0.25,
                "warmupInvocations": 1,
            },
        }
    )
    nodes = {node.id: SimpleNamespace(config=node) for node in settings.nodes}
    image = SimpleNamespace(id="sha256:" + "a" * 64)
    built = SimpleNamespace(
        source={"revision": "a" * 40}, function=lambda *_: SimpleNamespace(image=image)
    )
    topology = SimpleNamespace(
        resources=SimpleNamespace(nodes=nodes),
        function_settings=settings.functions,
        distribution="distribution",
        endpoints={key: key for key in nodes},
    )
    values = {
        "distribution": built,
        "clock": SimpleNamespace(require_healthy=lambda: None),
        **{key: "http://" + key for key in nodes},
    }
    value: Any = object()
    inputs = cast(
        Any, SimpleNamespace(resource=lambda key: values[key], upstream=lambda: value)
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.calibration.environment_identity",
        lambda *_: (
            "sha256:" + "b" * 64,
            {
                "host": "fake-test",
                "vm": "fake-vm",
                "os": "fake-os",
                "architecture": "fake-arch",
                "cpu": "fake-cpu",
            },
        ),
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.calibration.runtime_endpoint",
        lambda *_args, **kwargs: ("http://sdk", {}),
    )
    monkeypatch.setattr("nanolab.tasks.one_shot.calibration.time.sleep", lambda _: None)

    def respond(request):
        if request.url.path == "/invoke":
            return httpx.Response(200, json=7)
        if request.url.path == "/runtime/status":
            return httpx.Response(
                200, json={"physicalReleaseProof": True, "maxConcurrentHandlers": 1}
            )
        if request.url.path.startswith("/runtime/executions/"):
            execution = request.url.path.split("/")[-1]
            duration = (
                0.2
                if capacity_drift
                and execution.startswith("capacity-")
                and not execution.startswith("capacity-warmup-")
                else 0.1
            )
            return httpx.Response(
                200,
                json={
                    "executionId": execution,
                    "incarnation": "fake-runtime",
                    "state": "RELEASED",
                    "handlerStarted": True,
                    "dispatchAttempt": "1",
                    "occupancySeconds": duration,
                },
            )
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.calibration.httpx.Client",
            lambda **_: nullcontext(http),
        )
        value = (
            WarmupTask(topology, settings, tmp_path, clock=cast(Any, "clock"))
            .run(inputs)
            .value
        )
        value = (
            MeasureServiceTask(topology, settings, tmp_path, clock=cast(Any, "clock"))
            .run(inputs)
            .value
        )
        capacity = ValidateCapacityTask(
            topology, settings, tmp_path, clock=cast(Any, "clock")
        )
        if capacity_drift:
            with pytest.raises(ValueError, match="capacity/co-location"):
                capacity.run(inputs)
            assert (tmp_path / "capacity.jsonl").exists()
            assert not (tmp_path / "profile.json").exists()
        else:
            value = capacity.run(inputs).value
            profile = (
                PublishCalibrationTask(
                    topology, settings, tmp_path, clock=cast(Any, "clock")
                )
                .run(inputs)
                .value
            )
            assert profile is not None
            assert profile.functions[0]["serviceSeconds"] == 0.1
            assert len(value["capacity"]) == 6
            assert (tmp_path / "profile-reference.json").exists()
    assert len((tmp_path / "samples.jsonl").read_text().splitlines()) == 12


@pytest.mark.parametrize("expired", [False, True])
def test_capacity_readiness_retries_only_reads_and_preserves_deadline(
    tmp_path, monkeypatch, expired
):
    settings = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "nodes": [
                {"id": "edge-0", "kind": "edge"},
                {"id": "edge-1", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "functions": {"one-shot-workload": {"input": {}}},
        }
    )
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/runtime/status":
            if calls.count("/runtime/status") == 1:
                return httpx.Response(503)
            return httpx.Response(
                200, json={"physicalReleaseProof": True, "maxConcurrentHandlers": 1}
            )
        if request.url.path == "/invoke":
            return httpx.Response(200, json=7)
        return httpx.Response(
            200,
            json={
                "executionId": request.url.path.split("/")[-1],
                "incarnation": "fake-runtime",
                "dispatchAttempt": "1",
                "handlerStarted": True,
                "state": "RELEASED",
                "occupancySeconds": 0.1,
            },
        )

    monkeypatch.setattr("nanolab.tasks.one_shot.calibration.time.sleep", lambda _: None)
    if expired:
        times = iter([0, 31])
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.calibration.time.monotonic", lambda: next(times)
        )
    task = ValidateCapacityTask(object(), settings, tmp_path, clock=cast(Any, "clock"))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        if expired:
            with pytest.raises(httpx.HTTPStatusError, match="503"):
                task._warm_replica(
                    http,
                    "http://sdk",
                    "one-shot-workload",
                    {},
                    {"outputs": {"one-shot-workload": 7}},
                )
        else:
            task._warm_replica(
                http,
                "http://sdk",
                "one-shot-workload",
                {},
                {"outputs": {"one-shot-workload": 7}},
            )
            assert calls.count("/invoke") == 1
            assert (tmp_path / "warmup.jsonl").exists()
    assert calls.count("/runtime/status") == (1 if expired else 2)
