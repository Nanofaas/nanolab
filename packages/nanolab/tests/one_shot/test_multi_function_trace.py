"""Synchronized nine-round loads retain thirty distinct original flows."""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot.capacity import capacity_configurations
from nanolab.tasks.one_shot.trace import oracle_entries, request_schedule

GRAPH = {
    "edge-1": ["edge-2", "edge-3"],
    "edge-2": ["edge-1", "edge-3"],
    "edge-3": ["edge-1", "edge-2", "edge-4", "edge-5"],
    "edge-4": ["edge-3", "edge-5"],
    "edge-5": ["edge-3", "edge-4"],
}


def test_static_comparison_uses_balanced_feasible_vectors_not_individual_maxima():
    from nanolab.one_shot.capacity import comparison_replica_vectors

    settings, _records = inputs()
    vectors = comparison_replica_vectors(
        settings, {name: name for name in settings.functions}
    )
    assert set(vectors["cloud"].values()) == {7}
    assert sum(vectors["cloud"].values()) == 35
    assert set(vectors["edge-1"].values()) == {1}
    for node in settings.nodes:
        assert sum(vectors[node.id].values()) * 64 <= node.memory_capacity_mib


def test_warmup_covers_two_six_minute_periods_at_control_rates():
    from nanolab.config.one_shot import ExperimentSettings
    from nanolab.tasks.one_shot.trace import concurrent_trace, warmup_trace

    settings, records = inputs()
    trace = concurrent_trace(
        records,
        settings,
        period=360,
        seed=7,
        aliases={name: name for name in settings.functions},
    )
    assert trace.phase_ids is not None
    assert trace.requested_windows is not None
    experiment = ExperimentSettings.model_validate(
        {
            "warmupSeconds": 720,
            "windows": [
                {"id": phase, "epochs": 1, "rates": window}
                for phase, window in zip(trace.phase_ids, trace.windows, strict=True)
            ],
        }
    )
    assert experiment.warmup_seconds == 720
    warm = warmup_trace(trace, seconds=720)
    assert warm.period_seconds == 720 and warm.windows == [trace.windows[0]]
    assert warm.functions == trace.functions and warm.phase_ids == ["warmup"]
    with pytest.raises(ValueError, match="two periods"):
        warmup_trace(trace, seconds=600)


def inputs():
    names = [f"f{i}" for i in range(5)]
    nodes = []
    for i, (cpu, ram, multiple) in enumerate(
        [
            (1, 2048, 1),
            (1, 3072, 1),
            (2, 3072, 2),
            (2, 4096, 2),
            (3, 6144, 3),
            (8, 12288, 7),
        ]
    ):
        pool = 320 * multiple
        nodes.append(
            {
                "id": "cloud" if i == 5 else f"edge-{i + 1}",
                "kind": "cloud" if i == 5 else "edge",
                "cpus": cpu,
                "memoryMiB": ram,
                "memoryCapacityMiB": pool,
                "functionLimits": {
                    name: {
                        "cpu": int((cpu - 0.5) / (pool / 64) * 1000) / 1000,
                        "memoryMiB": 64,
                        "maxReplicas": pool // 64,
                    }
                    for name in names
                },
            }
        )
    settings = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "scientific-experiment",
            "strictMemory": True,
            "flowQuantum": 0.05,
            "nodes": nodes,
            "functions": {
                name: {
                    "input": {"seed": i},
                    "cpu": 0.1,
                    "memoryMiB": 64,
                    "maxReplicas": 5,
                }
                for i, name in enumerate(names)
            },
            "neighbors": GRAPH,
        }
    )
    configurations = capacity_configurations(settings, {name: name for name in names})
    records = []
    for configuration in configurations:
        if configuration["kind"] != "mixed":
            continue
        for node, vector in configuration["vectors"].items():
            position = [n.id for n in settings.nodes].index(node)
            speed = 4 if node == "cloud" else 1 + position / 10
            records.append(
                {
                    "node": node,
                    "replicaVector": vector,
                    "rates": {
                        name: count * speed * (0.6 + i * 0.1)
                        for i, (name, count) in enumerate(vector.items())
                    },
                    "trials": [],
                }
            )
    return settings, records


def test_nine_synchronized_windows_have_full_positive_matrix_and_distinct_hotspots():
    from nanolab.tasks.one_shot.trace import concurrent_trace

    settings, records = inputs()
    trace = concurrent_trace(
        records,
        settings,
        period=360,
        seed=7,
        aliases={name: name for name in settings.functions},
    )
    assert trace.phase_ids is not None
    assert trace.requested_windows is not None
    assert len(trace.windows) == 9 and trace.period_seconds == 360
    assert len(trace.windows) * trace.period_seconds == 54 * 60
    assert len(set(trace.phase_ids)) == 9
    assert all(
        len(window) == 6
        and all(
            len(rates) == 5 and min(rates.values()) > 0 for rates in window.values()
        )
        for window in trace.windows
    )
    assert trace.windows[0] != trace.windows[-1]
    for name in trace.functions:
        assert len({window["edge-1"][name] for window in trace.windows}) > 1
    assert {phase.removeprefix("hotspot-") for phase in trace.phase_ids[1:6]} == set(
        GRAPH
    )
    assert trace.phase_ids[6:8] == ["two-triangles", "diffuse-subset"]
    balanced = next(
        entry["vectors"]
        for entry in capacity_configurations(
            settings, {name: name for name in settings.functions}
        )
        if entry["kind"] == "mixed" and entry["dominant"] is None
    )
    for node in ["edge-1", "edge-3", "edge-5"]:
        capacity = next(
            record["rates"]
            for record in records
            if record["node"] == node and record["replicaVector"] == balanced[node]
        )
        assert all(
            trace.windows[7][node][name] >= 1.25 * capacity[name] - 1e-9
            for name in trace.functions
        )
    anchor = datetime(2026, 10, 10, tzinfo=UTC)
    assert (
        oracle_entries(
            trace,
            node="cloud",
            generations=dict.fromkeys(trace.functions, 1),
            anchor=anchor,
        )
        == []
    )
    entries = oracle_entries(
        trace,
        node="edge-3",
        generations=dict.fromkeys(trace.functions, 1),
        anchor=anchor,
    )
    assert len(entries) == 9 * 5
    assert entries[5]["start"] == entries[0]["end"]
    one = trace.model_copy(
        update={
            "windows": trace.windows[:1],
            "requested_windows": trace.requested_windows[:1],
            "phase_ids": trace.phase_ids[:1],
        }
    )
    scheduled = request_schedule(
        one,
        anchor=anchor,
        endpoints={node: "http://" + node for node in trace.nodes},
        run_id="round",
    )
    for node in trace.nodes:
        for name, rate in trace.windows[0][node].items():
            assert sum(
                row["origin"] == node and row["function"] == name for row in scheduled
            ) == round(360 * rate)


@pytest.mark.parametrize(
    "fault", ["scalar", "graph", "vector", "quantum", "period", "cloud-function"]
)
def test_invalid_scope_or_per_function_cloud_headroom_fails(fault):
    from nanolab.tasks.one_shot.trace import concurrent_trace

    settings, records = inputs()
    period = 360
    if fault == "scalar":
        records = {node.id: 10.0 for node in settings.nodes}
    elif fault == "graph":
        settings = settings.model_copy(
            update={"neighbors": {name: [] for name in GRAPH}}
        )
    elif fault == "vector":
        records = [record for record in records if record["node"] != "edge-1"]
    elif fault == "quantum":
        settings = settings.model_copy(update={"flow_quantum": 100})
    elif fault == "period":
        period = 60
    elif fault == "cloud-function":
        records = deepcopy(records)
        for record in records:
            if record["node"] == "cloud":
                record["rates"]["f0"] = 3.0
                for name in ["f1", "f2", "f3", "f4"]:
                    record["rates"][name] *= 100
    with pytest.raises(
        ValueError, match=r"scope|triangles|positive|period|headroom|capacity"
    ):
        concurrent_trace(
            cast(Any, records),
            settings,
            period=period,
            seed=7,
            aliases={name: name for name in settings.functions},
        )


@pytest.mark.parametrize("fault", [None, "missing-mix", "physical", "vector"])
def test_trace_requires_measured_cloud_mix_and_local_physical_headroom(fault):
    from types import SimpleNamespace

    from nanolab.one_shot.profiles import CalibrationProfiles, validate_trace_capacity
    from nanolab.tasks.one_shot.trace import (
        cloud_capacity_requirements,
        concurrent_trace,
    )

    settings, records = inputs()
    trace = concurrent_trace(
        records,
        settings,
        period=360,
        seed=7,
        aliases={name: name for name in settings.functions},
    )
    assert trace.phase_ids is not None
    assert trace.requested_windows is not None
    balanced = next(
        entry["vectors"]
        for entry in capacity_configurations(
            settings, {name: name for name in settings.functions}
        )
        if entry["kind"] == "mixed" and entry["dominant"] is None
    )
    if fault != "missing-mix":
        records += [
            {
                "node": "cloud",
                "replicaVector": dict.fromkeys(trace.functions, 1)
                if fault == "vector"
                else balanced["cloud"],
                "rates": rates,
                "trials": [],
            }
            for rates in cloud_capacity_requirements(trace, "cloud")
        ]
    profiles = CalibrationProfiles(
        by_node={
            node.id: cast(
                Any,
                SimpleNamespace(
                    functions=[
                        {
                            "function": name,
                            "serviceSeconds": 5.0 if fault == "physical" else 0.01,
                            "memoryMiB": 64,
                            "validity": {"maxReplicas": 35},
                        }
                        for name in trace.functions
                    ]
                ),
            )
            for node in settings.nodes
        },
        sha256="a" * 64,
        environment_fingerprint="sha256:" + "a" * 64,
        concurrent_capacities=records,
    )
    if fault is None:
        validate_trace_capacity(trace, profiles, settings)
    else:
        with pytest.raises(ValueError, match=r"mix|headroom|vector"):
            validate_trace_capacity(trace, profiles, settings)


def test_cloud_mix_acquisition_uses_fixed_feasible_vector_and_raw_hashes(
    tmp_path, monkeypatch
):
    import json
    from types import SimpleNamespace
    from typing import Any, cast

    from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash
    from nanolab.tasks.one_shot.calibration import ValidateTraceCapacityTask
    from nanolab.tasks.one_shot.trace import (
        cloud_capacity_requirements,
        concurrent_trace,
    )

    settings, records = inputs()
    value = {
        "measuredConcurrentCapacity": records,
        "statistics": {
            f"{node.id}/{name}": {"meanSeconds": 0.01}
            for node in settings.nodes
            for name in settings.functions
        },
    }
    topology = SimpleNamespace(
        function_settings=settings.functions,
        function_aliases={name: name for name in settings.functions},
    )
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
        assert set(replicas) == {"cloud"}
        vector = replicas["cloud"]
        assert set(vector.values()) == {7}
        rows = [
            {
                "node": "cloud",
                "function": name,
                "replicas": count,
                "replicaVector": vector,
                "mix": rates["cloud"],
                "rate": rates["cloud"][name],
                "repetition": repetition,
                "warmupSeconds": 30,
                "measurementSeconds": 60,
                "planned": 100,
                "emitted": 100,
                "physicalCompletions": 100,
                "censored": 0,
                "generatorDeficit": 0,
                "validOutput": True,
                "relativeServiceDrift": 0.0,
                "maxArrivalLatenessSeconds": 0.001,
            }
            for name, count in vector.items()
        ]
        run_dir.mkdir(parents=True)
        (run_dir / "trial.json").write_bytes(canonical_bytes(rows))
        return rows

    monkeypatch.setattr("nanolab.tasks.one_shot.calibration.run_capacity_trial", trial)
    task = ValidateTraceCapacityTask(
        topology, settings, tmp_path, clock=cast(Any, "clock")
    )
    result = task.run(cast(Any, SimpleNamespace(upstream=lambda: value))).value
    assert result is not None
    trace = concurrent_trace(
        records,
        settings,
        period=360,
        seed=settings.seed,
        aliases=topology.function_aliases,
    )
    assert trace.phase_ids is not None
    assert trace.requested_windows is not None
    requirements = cloud_capacity_requirements(trace, "cloud")
    assert len(calls) == 9 * 3
    assert {canonical_bytes(rates["cloud"]) for _replicas, rates in calls} == {
        canonical_bytes(rates) for rates in requirements
    }
    for record in result["measuredConcurrentCapacity"][-9:]:
        for reference in record["trials"]:
            assert (
                content_hash((tmp_path / reference["path"]).read_bytes())
                == reference["sha256"]
            )
    plan = json.loads((tmp_path / "trace-capacity-plan.json").read_bytes())
    assert plan["minimumWallSeconds"] == 27 * 90


@pytest.mark.parametrize(
    "fault",
    [None, "period", "samples", "quantile", "epsilon", "rates", "order", "cells"],
)
def test_synchronized_timing_scope_is_checked_before_acquisition(fault):
    from types import SimpleNamespace
    from typing import Any, cast

    from nanolab.config.one_shot import TimingSettings
    from nanolab.one_shot.profiles import CalibrationProfiles, synchronized_timing_trace
    from nanolab.tasks.one_shot.trace import (
        cloud_capacity_requirements,
        concurrent_trace,
    )

    settings, records = inputs()
    trace = concurrent_trace(
        records,
        settings,
        period=360,
        seed=7,
        aliases={name: name for name in settings.functions},
    )
    assert trace.phase_ids is not None
    assert trace.requested_windows is not None
    records += [
        {
            "node": "cloud",
            "replicaVector": dict.fromkeys(trace.functions, 7),
            "rates": rates,
            "trials": [],
        }
        for rates in cloud_capacity_requirements(trace, "cloud")
    ]
    profiles = CalibrationProfiles(
        {
            node.id: cast(
                Any,
                SimpleNamespace(
                    functions=[
                        {
                            "function": name,
                            "serviceSeconds": 0.01,
                            "memoryMiB": 64,
                            "validity": {"maxReplicas": 35},
                        }
                        for name in trace.functions
                    ]
                ),
            )
            for node in settings.nodes
        },
        "a" * 64,
        "sha256:" + "a" * 64,
        concurrent_capacities=records,
    )
    data = {
        "periodCandidatesSeconds": [360],
        "maxTraceResolutionSeconds": 360,
        "minSamples": 50,
        "quantile": 0.95,
        "epsilon": 0.05,
        "measurementLeadSeconds": 2,
        "protocol": {"preparationSeconds": 1},
        "matrix": [
            {"id": phase, "rates": deepcopy(window)}
            for phase, window in zip(trace.phase_ids, trace.windows, strict=True)
        ],
    }
    if fault == "period":
        data["periodCandidatesSeconds"] = [60]
    elif fault == "samples":
        data["minSamples"] = 49
    elif fault == "quantile":
        data["quantile"] = 0.9
    elif fault == "epsilon":
        data["epsilon"] = 0.1
    elif fault == "rates":
        data["matrix"][0]["rates"]["edge-1"]["f0"] += 0.05
    elif fault == "order":
        data["matrix"][1:3] = list(reversed(data["matrix"][1:3]))
    elif fault == "cells":
        data["matrix"].pop()
    settings = settings.model_copy(
        update={"timing": TimingSettings.model_validate(data)}
    )
    if fault is None:
        assert synchronized_timing_trace(settings, profiles) == trace
    else:
        with pytest.raises(ValueError, match=r"synchronized|matrix|sampling|period"):
            synchronized_timing_trace(settings, profiles)
