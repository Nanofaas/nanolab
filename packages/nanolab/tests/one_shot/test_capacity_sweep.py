"""Capacity requires three sustained open-loop measurements and no hidden loss."""

from importlib import import_module

import pytest


def trials():
    return [
        {
            "node": "edge-1",
            "replicas": 2,
            "rate": rate,
            "repetition": repetition,
            "warmupSeconds": 30,
            "measurementSeconds": 60,
            "planned": 600,
            "emitted": 600,
            "physicalCompletions": completions,
            "censored": 0,
            "generatorDeficit": 0,
            "validOutput": True,
            "relativeServiceDrift": 0.02,
        }
        for rate, completions in [(8.0, 600), (10.0, 597), (12.0, 590)]
        for repetition in range(3)
    ]


def test_highest_rate_needs_three_successful_trials():
    capacity = import_module("nanolab.one_shot.capacity")
    assert (
        capacity.select_capacity(trials(), node="edge-1", replicas=2, tolerance=0.1)
        == 10.0
    )


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "duplicate",
        "deficit",
        "censored",
        "drift",
        "short",
        "wrong-replicas",
        "output",
    ],
)
def test_unqualified_trial_cannot_raise_measured_capacity(bad):
    capacity = import_module("nanolab.one_shot.capacity")
    rows = trials()
    if bad == "missing":
        rows.pop(4)
    if bad == "duplicate":
        rows[4]["repetition"] = 0
    if bad == "deficit":
        rows[4]["generatorDeficit"] = 1
    if bad == "censored":
        rows[4]["censored"] = 1
    if bad == "drift":
        rows[4]["relativeServiceDrift"] = 0.2
    if bad == "short":
        rows[4]["measurementSeconds"] = 59
    if bad == "wrong-replicas":
        rows[4]["replicas"] = 1
    if bad == "output":
        rows[4]["validOutput"] = False
    assert (
        capacity.select_capacity(rows, node="edge-1", replicas=2, tolerance=0.1) == 8.0
    )


def test_no_supported_capacity_is_explicit_failure():
    capacity = import_module("nanolab.one_shot.capacity")
    with pytest.raises(ValueError, match="NOT_QUALIFIED"):
        capacity.select_capacity([], node="edge-1", replicas=2, tolerance=0.1)


@pytest.mark.parametrize("limited", [False, True])
def test_capacity_range_uses_local_limits_and_all_nodes_simultaneously(
    tmp_path, monkeypatch, limited
):
    from types import SimpleNamespace
    from typing import Any, cast

    from nanolab.config.one_shot import OneShotConfig
    from nanolab.tasks.one_shot.calibration import ValidateCapacityTask

    settings = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "scientific-experiment",
            "nodes": [
                {
                    "id": "edge-1",
                    "kind": "edge",
                    "cpus": 1,
                    "memoryCapacityMiB": 128,
                    "functionLimits": {
                        "f": {"cpu": 0.35, "memoryMiB": 128, "maxReplicas": 1}
                    },
                },
                {
                    "id": "edge-2",
                    "kind": "edge",
                    "cpus": 1,
                    "memoryCapacityMiB": 128,
                    "functionLimits": {
                        "f": {"cpu": 0.5, "memoryMiB": 128, "maxReplicas": 1}
                    },
                },
                {
                    "id": "cloud",
                    "kind": "cloud",
                    "cpus": 4,
                    "memoryCapacityMiB": 384,
                    "functionLimits": {
                        "f": {"cpu": 1, "memoryMiB": 128, "maxReplicas": 3}
                    },
                },
            ],
            "functions": {"f": {"input": {"seed": 7}}},
        }
    )
    topology = SimpleNamespace(
        function_aliases={"f": "f"}, function_settings=settings.functions
    )
    value = {
        "statistics": {node.id + "/f": {"meanSeconds": 0.1} for node in settings.nodes},
        "capacity": [],
    }
    inputs = cast(Any, SimpleNamespace(upstream=lambda: value))
    calls = []

    def trial(
        _topology,
        _settings,
        _inputs,
        _value,
        *,
        replicas,
        rates,
        repetition,
        run_dir,
        clock,
    ):
        calls.append((dict(replicas), dict(rates)))
        return [
            {
                "node": node,
                "replicas": replicas[node],
                "rate": rate,
                "repetition": repetition,
                "warmupSeconds": 30,
                "measurementSeconds": 60,
                "planned": 600,
                "emitted": 600,
                "physicalCompletions": 300
                if limited and rate > replicas[node] * 5
                else 600,
                "censored": 0,
                "generatorDeficit": 0,
                "validOutput": True,
                "relativeServiceDrift": 0.0,
            }
            for node, rate in rates.items()
        ]

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.calibration.run_capacity_trial", trial, raising=False
    )
    task = ValidateCapacityTask(topology, settings, tmp_path, clock=cast(Any, "clock"))
    if limited:
        with pytest.raises(ValueError, match="capacity model"):
            task.run(inputs)
        return
    result = task.run(inputs).value
    assert result is not None
    assert result["measuredCapacity"]["cloud"] == 37.5
    assert {next(iter(rep.values())) for rep, _ in calls if set(rep) == {"cloud"}} == {
        1,
        2,
        3,
    }
    assert all(rep["edge-1"] == 1 for rep, _ in calls if "edge-1" in rep)
    assert sum(set(rep) == {"edge-1", "edge-2", "cloud"} for rep, _ in calls) == 3


@pytest.mark.parametrize(
    "outcome", ["complete", "missing-proof", "wrong-output", "generator-error"]
)
def test_open_loop_trial_requires_sdk_release_and_preserves_generator_failure(
    tmp_path, monkeypatch, outcome
):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from typing import Any, cast

    import httpx

    from nanolab.config.one_shot import OneShotConfig
    from nanolab.tasks.one_shot import experiment
    from nanolab.tasks.one_shot.capacity import run_capacity_trial

    config = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "scientific-experiment",
            "nodes": [
                {"id": "edge-1", "kind": "edge"},
                {"id": "edge-2", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "functions": {"f": {"input": {}}},
        }
    )
    topology = SimpleNamespace(
        function_settings=config.functions,
        resources=SimpleNamespace(nodes={"edge-1": "node"}),
        endpoints={"edge-1": "endpoint"},
    )
    clock = SimpleNamespace(require_healthy=lambda: None)
    inputs = cast(
        Any,
        SimpleNamespace(
            resource=lambda resource: (
                clock if resource == "clock" else "http://gateway:8080"
            )
        ),
    )
    value = {"outputs": {"f": 42}, "statistics": {"edge-1/f": {"meanSeconds": 0.1}}}
    process = SimpleNamespace(
        poll=lambda: 0, returncode=1 if outcome == "generator-error" else 0
    )
    generator = SimpleNamespace(acquire=lambda _: process, release=lambda *_: None)
    monkeypatch.setattr(experiment, "generator_resource", lambda **_: generator)
    monkeypatch.setattr(
        experiment,
        "LiveEvidenceCollector",
        lambda *_: SimpleNamespace(close=lambda: None, require_healthy=lambda: None),
    )
    monkeypatch.setattr(experiment, "collect_runtime_evidence", lambda *_: None)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.runtime_endpoint",
        lambda *_args, **_kwargs: ("http://sdk", {}),
    )

    def respond(request):
        if request.url.path == "/invoke":
            return httpx.Response(200, json=42)
        if request.url.path.startswith("/runtime/executions/"):
            return httpx.Response(
                200,
                json={
                    "executionId": request.url.path.rsplit("/", 1)[1],
                    "incarnation": "sdk",
                    "dispatchAttempt": "1",
                    "state": "RELEASED",
                    "handlerStarted": True,
                    "occupancySeconds": 0.1,
                },
            )
        return httpx.Response(
            200, json={"maxConcurrentHandlers": 1, "physicalReleaseProof": True}
        )

    directory = tmp_path / "trial"

    def originals(_path):
        import json

        scheduled = json.loads((directory / "schedule.json").read_bytes())
        return [
            record
            for row in scheduled
            for record in (
                {**row, "event": "emitted", "emittedAt": row["scheduledAt"]},
                {
                    **row,
                    "event": "result",
                    "emittedAt": row["scheduledAt"],
                    "statusCode": 200,
                    "responseStatus": "success",
                    "outputJson": "41" if outcome == "wrong-output" else "42",
                    "responseBody": "{}",
                },
            )
        ]

    def proofs(path):
        if outcome == "missing-proof":
            return []
        return [
            {
                **row,
                "destination": "edge-1",
                "state": "RELEASED",
                "handlerStarted": True,
                "occupancySeconds": 0.1,
            }
            for row in originals(path)
            if row["event"] == "result"
        ]

    monkeypatch.setattr(experiment, "generator_rows", originals)
    monkeypatch.setattr(experiment, "physical_attempts", proofs)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.capacity.httpx.Client",
            lambda **_: nullcontext(http),
        )
        if outcome == "generator-error":
            with pytest.raises(RuntimeError, match="generator failed"):
                run_capacity_trial(
                    topology,
                    config,
                    inputs,
                    value,
                    replicas={"edge-1": 1},
                    rates={"edge-1": 1},
                    repetition=0,
                    run_dir=directory,
                    clock="clock",
                )
            assert "generator failed" in (directory / "failure.txt").read_text()
        else:
            rows = run_capacity_trial(
                topology,
                config,
                inputs,
                value,
                replicas={"edge-1": 1},
                rates={"edge-1": 1},
                repetition=0,
                run_dir=directory,
                clock="clock",
            )
            assert rows[0]["planned"] == 60
            assert rows[0]["physicalCompletions"] == (
                60 if outcome == "complete" else 0
            )
            assert rows[0]["censored"] == (0 if outcome == "complete" else 60)
            assert rows[0]["validOutput"] is (outcome != "wrong-output")
            assert (directory / "trial.json").is_file()
