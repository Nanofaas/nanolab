"""Memory-feasible allocation bounds preserve CPU and platform reservations."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from nanolab.config.one_shot import OneShotConfig


def multi_settings(cpu=0.1, pool=320):
    return {
        "provider": "multipass",
        "purpose": "scientific-experiment",
        "strictMemory": True,
        "nodes": [
            {
                "id": name,
                "kind": kind,
                "cpus": 1,
                "memoryMiB": 2048,
                "memoryCapacityMiB": pool,
            }
            for name, kind in [("e1", "edge"), ("e2", "edge"), ("cloud", "cloud")]
        ],
        "functions": {
            f"f{i}": {
                "input": {},
                "cpu": cpu,
                "memoryMiB": 64,
                "maxReplicas": pool // 64,
            }
            for i in range(5)
        },
    }


def test_individual_maxima_do_not_imply_simultaneous_cpu_overcommit():
    # Five individual maxima charge 2.5 CPU, but RAM permits five replicas TOTAL.
    settings = OneShotConfig.model_validate(multi_settings())
    assert len(settings.functions) == 5


def test_memory_feasible_cpu_overcommit_is_still_rejected():
    with pytest.raises(ValidationError, match="CPU"):
        OneShotConfig.model_validate(multi_settings(cpu=0.21))


def test_initial_five_functions_must_fit_simultaneously():
    data = multi_settings(pool=256)
    with pytest.raises(ValidationError, match=r"initial|simultaneous"):
        OneShotConfig.model_validate(data)


def test_control_plane_and_os_are_reserved_before_accepting_replica_cpu():
    data = multi_settings()
    data["functions"] = {
        "only": {
            "input": {},
            "cpu": 0.7,
            "memoryMiB": 320,
            "maxReplicas": 1,
        }
    }
    with pytest.raises(ValidationError, match=r"CPU|reservation"):
        OneShotConfig.model_validate(data)


def test_collector_reservation_is_checked_against_feasible_replica_bound():
    data = multi_settings(cpu=0.09)
    data["observability"] = {
        "logs": {
            "enabled": True,
            "campaignId": "capacity-test",
            "endpoint": "http://10.0.0.1:3100/loki/api/v1/push",
            "image": "fluent/fluent-bit:5.1.3",
        }
    }
    settings = OneShotConfig.model_validate(data)
    assert settings.observability.logs.enabled
    unsafe = deepcopy(data)
    unsafe["functions"]["f0"]["cpu"] = 0.11
    with pytest.raises(ValidationError, match=r"CPU|reservation"):
        OneShotConfig.model_validate(unsafe)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "wrong-output",
        "wrong-function",
        "duplicate",
        "deficit",
        "late",
        "telemetry",
        "telemetry-off",
        "generator-error",
        "generator-cleanup-error",
        "swap",
        "oom",
    ],
)
def test_two_functions_keep_distinct_runtime_payload_output_and_accounting(
    tmp_path, monkeypatch, fault
):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    from typing import Any, cast

    import httpx

    from nanolab.tasks.one_shot import experiment
    from nanolab.tasks.one_shot.capacity import run_capacity_trial

    settings = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "strictMemory": fault in {"swap", "oom"},
            "nodes": [
                {"id": "e1", "kind": "edge"},
                {"id": "e2", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "functions": {
                "f0": {"input": {"seed": 1}, "cpu": 0.05},
                "f1": {"input": {"seed": 2}, "cpu": 0.05},
            },
        }
    )
    node = SimpleNamespace(
        command=lambda *args: SimpleNamespace(stdout=""),
        request=SimpleNamespace(name="run-e1"),
    )
    topology = SimpleNamespace(
        function_settings=settings.functions,
        endpoints={"e1": "endpoint"},
        resources=SimpleNamespace(nodes={"e1": node}),
        function_aliases={"f0": "f0", "f1": "f1"},
    )
    clock = SimpleNamespace(require_healthy=lambda: None)
    inputs = cast(
        Any,
        SimpleNamespace(
            resource=lambda resource: clock if resource == "clock" else "http://gateway"
        ),
    )
    value = {
        "outputs": {"f0": 1, "f1": 2},
        "statistics": {"e1/f0": {"meanSeconds": 0.1}, "e1/f1": {"meanSeconds": 0.1}},
    }
    released = []
    process = SimpleNamespace(
        poll=lambda: 0,
        returncode=1 if fault in {"generator-error", "generator-cleanup-error"} else 0,
    )
    telemetry_actions = []

    def generator(**kwargs):
        if fault in {"telemetry", "telemetry-off"}:
            assert kwargs["duration"] == 135
        return SimpleNamespace(
            acquire=lambda _: telemetry_actions.append("generator") or process,
            release=lambda *args: released.append(True),
        )

    monkeypatch.setattr(experiment, "generator_resource", generator)
    monkeypatch.setattr(
        "nanolab.one_shot.vm_resources.VmTelemetrySession",
        lambda *args: (
            telemetry_actions.append("sampler")
            or SimpleNamespace(
                close=lambda: telemetry_actions.append("sampler-close"),
                require_healthy=lambda: None,
            )
        ),
    )

    def close():
        if fault == "generator-cleanup-error":
            raise RuntimeError("collector cleanup failed")

    monkeypatch.setattr(
        experiment,
        "LiveEvidenceCollector",
        lambda *args: SimpleNamespace(close=close, require_healthy=lambda: None),
    )
    monkeypatch.setattr(experiment, "collect_runtime_evidence", lambda *args: None)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.runtime_endpoint",
        lambda node, function, **kwargs: (
            "http://sdk-" + function,
            {"HostConfig": {"MemorySwap": 128 * 1024 * 1024}},
        ),
    )
    observed_cgroups = []

    def cgroup(*args):
        observed_cgroups.append(True)
        return {
            "memory.max": str(128 * 1024 * 1024),
            "memory.swap.max": "1" if fault == "swap" else "0",
            "memory.current": "1024",
            "memory.peak": "2048",
            "cpu.max": "5000 100000",
            "memory.events": "oom 1\noom_kill 1"
            if fault == "oom" and len(observed_cgroups) > 2
            else "oom 0\noom_kill 0",
        }

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.collect_container_cgroup", cgroup
    )

    def respond(request):
        function = request.url.host.removeprefix("sdk-")
        if request.url.path == "/invoke":
            assert json.loads(request.content)["input"] == {
                "seed": value["outputs"][function]
            }
            return httpx.Response(200, json=value["outputs"][function])
        if request.url.path.startswith("/runtime/executions/"):
            return httpx.Response(
                200,
                json={
                    "executionId": request.url.path.rsplit("/", 1)[-1],
                    "incarnation": "sdk-" + function,
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
        schedule = json.loads((directory / "schedule.json").read_bytes())
        emitted = [
            dict(
                row,
                event="emitted",
                emittedAt=row["scheduledAt"] + (0.5 if fault == "late" else 0),
            )
            for row in schedule
        ]
        results = [
            dict(
                row,
                event="result",
                emittedAt=row["scheduledAt"],
                statusCode=200,
                responseStatus="success",
                outputJson=str(
                    999
                    if fault == "wrong-output" and row["function"] == "f1"
                    else value["outputs"][row["function"]]
                ),
                responseBody="{}",
            )
            for row in schedule
        ]
        if fault == "duplicate":
            emitted += emitted[-1:]
        if fault == "deficit":
            emitted = emitted[:-1]
        return emitted + results

    def proofs(path):
        return [
            dict(
                row,
                destination="e1",
                state="RELEASED",
                handlerStarted=True,
                occupancySeconds=0.1,
                function="f0" if fault == "wrong-function" else row["function"],
            )
            for row in originals(path)
            if row["event"] == "result"
        ]

    monkeypatch.setattr(experiment, "generator_rows", originals)
    monkeypatch.setattr(experiment, "physical_attempts", proofs)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.capacity.httpx.Client",
            lambda **kwargs: nullcontext(http),
        )
        if fault in {"swap", "oom"}:
            with pytest.raises(ValueError, match=r"swap|OOM"):
                run_capacity_trial(
                    topology,
                    settings,
                    inputs,
                    value,
                    replicas={"e1": {"f0": 1, "f1": 1}},
                    rates={"e1": {"f0": 1.0, "f1": 2.0}},
                    repetition=0,
                    run_dir=directory,
                    clock="clock",
                )
            assert released == ([] if fault == "swap" else [True])
            assert (directory / "failure.txt").exists()
            return
        if fault in {"generator-error", "generator-cleanup-error"}:
            with pytest.raises(RuntimeError, match="generator failed"):
                run_capacity_trial(
                    topology,
                    settings,
                    inputs,
                    value,
                    replicas={"e1": {"f0": 1, "f1": 1}},
                    rates={"e1": {"f0": 1.0, "f1": 2.0}},
                    repetition=0,
                    run_dir=directory,
                    clock="clock",
                )
            assert released == [True]
            assert "generator failed" in (directory / "failure.txt").read_text()
            return
        rows = run_capacity_trial(
            topology,
            settings,
            inputs,
            value,
            replicas={"e1": {"f0": 1, "f1": 1}},
            rates={"e1": {"f0": 1.0, "f1": 2.0}},
            repetition=0,
            run_dir=directory,
            clock="clock",
            telemetry_enabled=fault == "telemetry",
            preload_lead_seconds=45 if fault == "telemetry-off" else None,
        )
    assert released == [True]
    assert {row["function"] for row in rows} == {"f0", "f1"}
    by_name = {row["function"]: row for row in rows}
    assert by_name["f0"]["planned"] == 60
    assert by_name["f1"]["planned"] == 120
    assert all(row["replicaVector"] == {"f0": 1, "f1": 1} for row in rows)
    if fault in {None, "telemetry", "telemetry-off"}:
        if fault == "telemetry":
            assert telemetry_actions == ["sampler", "generator", "sampler-close"]
        if fault == "telemetry-off":
            assert telemetry_actions == ["generator"]
        assert all(
            row["validOutput"] and row["physicalCompletions"] == row["planned"]
            for row in rows
        )
    else:
        assert any(
            not row["validOutput"] or row["generatorDeficit"] or row["censored"]
            for row in rows
        )
    assert all(row["physicalCompletions"] <= row["emitted"] for row in rows)


def mixed_trials():
    return [
        {
            "node": "e1",
            "function": name,
            "replicas": 1,
            "replicaVector": {"f0": 1, "f1": 1},
            "mix": {"f0": 2.0, "f1": 3.0},
            "rate": rate,
            "repetition": repetition,
            "warmupSeconds": 30,
            "measurementSeconds": 60,
            "planned": 120 if name == "f0" else 180,
            "emitted": 120 if name == "f0" else 180,
            "physicalCompletions": 120 if name == "f0" else 180,
            "generatorDeficit": 0,
            "censored": 0,
            "validOutput": True,
            "relativeServiceDrift": 0.0,
            "maxArrivalLatenessSeconds": 0.01,
        }
        for repetition in range(3)
        for name, rate in [("f0", 2.0), ("f1", 3.0)]
    ]


@pytest.mark.parametrize(
    "fault", [None, "missing", "duplicate", "mix", "vector", "output", "late"]
)
def test_mixed_capacity_requires_joint_three_repetitions(fault):
    from nanolab.one_shot.capacity import select_mixed_capacity

    rows = mixed_trials()
    if fault == "missing":
        rows.pop()
    elif fault == "duplicate":
        rows[-1]["repetition"] = 0
    elif fault == "mix":
        rows[-1]["mix"] = {"f0": 4.0, "f1": 3.0}
    elif fault == "vector":
        rows[-1]["replicaVector"] = {"f0": 2, "f1": 1}
    elif fault == "output":
        rows[-1]["validOutput"] = False
    elif fault == "late":
        rows[-1]["maxArrivalLatenessSeconds"] = 0.3
    if fault is None:
        assert select_mixed_capacity(
            rows,
            node="e1",
            vector={"f0": 1, "f1": 1},
            rates={"f0": 2.0, "f1": 3.0},
            tolerance=0.15,
        ) == {"f0": 2.0, "f1": 3.0}
    else:
        with pytest.raises(ValueError, match="NOT_QUALIFIED"):
            select_mixed_capacity(
                rows,
                node="e1",
                vector={"f0": 1, "f1": 1},
                rates={"f0": 2.0, "f1": 3.0},
                tolerance=0.15,
            )


@pytest.mark.parametrize("fault", [None, "hash", "rate", "vector", "escape", "missing"])
def test_concurrent_artifact_binds_exact_trials(tmp_path, fault):
    import json

    from nanolab.one_shot.capacity import read_concurrent_capacity
    from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash

    references = []
    for repetition in range(3):
        path = tmp_path / f"p{repetition}.json"
        path.write_bytes(
            canonical_bytes(
                [row for row in mixed_trials() if row["repetition"] == repetition]
            )
        )
        references.append(
            {"path": path.name, "sha256": content_hash(path.read_bytes())}
        )
    record = {
        "node": "e1",
        "replicaVector": {"f0": 1, "f1": 1},
        "rates": {"f0": 2.0, "f1": 3.0},
        "trials": references,
    }
    raw = {
        "schemaVersion": 2,
        "artifactType": "measured-concurrent-capacity",
        "environmentFingerprint": "sha256:" + "a" * 64,
        "functions": ["f0", "f1"],
        "capacities": [record],
    }
    if fault == "hash":
        (tmp_path / "p0.json").write_text("[]")
    elif fault == "rate":
        record["rates"]["f1"] = 4.0
    elif fault == "vector":
        record["replicaVector"]["f1"] = 2
    elif fault == "escape":
        references[0]["path"] = "../p0.json"
    elif fault == "missing":
        references.pop()
    kwargs = {
        "root": tmp_path,
        "nodes": {"e1"},
        "functions": {"f0", "f1"},
        "fingerprint": raw["environmentFingerprint"],
        "tolerance": 0.15,
    }
    if fault is None:
        records = read_concurrent_capacity(json.loads(canonical_bytes(raw)), **kwargs)
        assert records[0]["rates"] == {"f0": 2.0, "f1": 3.0}
    else:
        with pytest.raises(
            ValueError, match=r"hash|dossier|qualified|QUALIFIED|record"
        ):
            read_concurrent_capacity(raw, **kwargs)


def test_capacity_plan_batches_pure_levels_and_feasible_distinct_mixes():
    from nanolab.config.one_shot import resolve_node_functions
    from nanolab.one_shot.capacity import capacity_configurations

    data = multi_settings(cpu=0.09)
    data["nodes"][-1].update(cpus=2, memoryCapacityMiB=640)
    data["nodes"][-1]["functionLimits"] = {
        name: {"cpu": 0.1, "memoryMiB": 64, "maxReplicas": 10}
        for name in data["functions"]
    }
    settings = OneShotConfig.model_validate(data)
    plans = capacity_configurations(
        settings, {name: name for name in settings.functions}
    )
    pure = [entry for entry in plans if entry["kind"] == "pure"]
    assert len(pure) == 5 * 10
    assert all(
        set(entry["vectors"]) == {"cloud"} for entry in pure if entry["level"] > 5
    )
    for node in settings.nodes:
        local = resolve_node_functions(settings, node.id)
        for name, function in local.items():
            assert {
                entry["vectors"][node.id][name]
                for entry in pure
                if entry["function"] == name and node.id in entry["vectors"]
            } == set(range(1, function.max_replicas + 1))
        mixed = [
            entry["vectors"][node.id] for entry in plans if entry["kind"] == "mixed"
        ]
        assert all(all(vector.values()) for vector in mixed)
        assert all(
            sum(vector[name] * local[name].memory_mib for name in local)
            <= node.memory_capacity_mib
            for vector in mixed
        )
    assert (
        len(
            {
                tuple(entry["vectors"]["cloud"].values())
                for entry in plans
                if entry["kind"] == "mixed"
            }
        )
        == 6
    )


def test_scientific_multi_calibration_publishes_joint_trials_and_cost(
    tmp_path, monkeypatch
):
    import json
    from types import SimpleNamespace
    from typing import Any, cast

    from nanolab.one_shot.capacity import read_concurrent_capacity
    from nanolab.tasks.one_shot.artifacts import canonical_bytes
    from nanolab.tasks.one_shot.calibration import ValidateCapacityTask

    settings = OneShotConfig.model_validate(multi_settings())
    topology = SimpleNamespace(
        function_settings=settings.functions,
        function_aliases={name: name for name in settings.functions},
    )
    value: dict[str, Any] = {
        "statistics": {
            f"{node.id}/{name}": {"meanSeconds": 0.1}
            for node in settings.nodes
            for name in settings.functions
        }
    }
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
        calls.append((replicas, rates))
        rows = [
            {
                "node": node,
                "function": name,
                "replicas": count,
                "replicaVector": vector,
                "mix": rates[node],
                "rate": rates[node][name],
                "repetition": repetition,
                "warmupSeconds": 30,
                "measurementSeconds": 60,
                "planned": 600,
                "emitted": 600,
                "physicalCompletions": 600,
                "generatorDeficit": 0,
                "censored": 0,
                "validOutput": True,
                "relativeServiceDrift": 0.0,
                "maxArrivalLatenessSeconds": 0.001,
            }
            for node, vector in replicas.items()
            for name, count in vector.items()
            if count
        ]
        run_dir.mkdir(parents=True)
        (run_dir / "trial.json").write_bytes(canonical_bytes(rows))
        return rows

    monkeypatch.setattr("nanolab.tasks.one_shot.calibration.run_capacity_trial", trial)
    task = ValidateCapacityTask(topology, settings, tmp_path, clock=cast(Any, "clock"))
    result = task.run(cast(Any, SimpleNamespace(upstream=lambda: value))).value
    assert result is not None
    assert "measuredCapacity" not in result
    records = result["measuredConcurrentCapacity"]
    raw = {
        "schemaVersion": 2,
        "artifactType": "measured-concurrent-capacity",
        "environmentFingerprint": "sha256:" + "a" * 64,
        "functions": list(settings.functions),
        "capacities": records,
    }
    assert (
        read_concurrent_capacity(
            raw,
            root=tmp_path,
            nodes={node.id for node in settings.nodes},
            functions=set(settings.functions),
            fingerprint=raw["environmentFingerprint"],
            tolerance=0.15,
        )
        == records
    )
    plan = json.loads((tmp_path / "capacity-plan.json").read_bytes())
    assert plan["minimumTrialWallSeconds"] == len(calls) * 90
    assert all(
        set(vector) == set(settings.functions)
        for replicas, _ in calls
        for vector in replicas.values()
    )
    assert any(
        all(all(vector.values()) for vector in replicas.values())
        for replicas, _ in calls
    )
    from nanolab.config.one_shot import ArtifactReference
    from nanolab.one_shot.profiles import read_profiles
    from nanolab.tasks.one_shot.artifacts import content_hash
    from nanolab.tasks.one_shot.calibration import PublishCalibrationTask

    value.update(
        source={"revision": "a" * 40},
        fingerprint="sha256:" + "a" * 64,
        nodeFingerprints={node.id: "sha256:" + "b" * 64 for node in settings.nodes},
        nodeEnvironments={
            node.id: {
                "host": "test",
                "vm": "multipass",
                "os": "linux",
                "architecture": "aarch64",
                "cpu": "test",
            }
            for node in settings.nodes
        },
        samples={
            f"{node.id}/{name}": [
                {
                    "node": node.id,
                    "function": name,
                    "state": "complete",
                    "occupancySeconds": 0.1,
                }
                for _ in range(50)
            ]
            for node in settings.nodes
            for name in settings.functions
        },
    )
    topology.distribution = "distribution"
    built = SimpleNamespace(
        function=lambda *args: SimpleNamespace(
            image=SimpleNamespace(id="sha256:" + "c" * 64)
        )
    )
    publisher = PublishCalibrationTask(
        topology, settings, tmp_path, clock=cast(Any, "clock")
    )
    publisher.run(
        cast(Any, SimpleNamespace(upstream=lambda: value, resource=lambda _: built))
    )
    path = tmp_path / "profile.json"
    calibrated = read_profiles(
        settings.model_copy(
            update={
                "profile": ArtifactReference(
                    path=path, sha256=content_hash(path.read_bytes())
                )
            }
        )
    )
    assert calibrated.concurrent_capacities == records
    assert calibrated.capacities is None
