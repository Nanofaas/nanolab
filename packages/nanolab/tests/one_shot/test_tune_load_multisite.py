"""Multi-site load design: copied nodes, ramped hotspots and their smoothness."""

import importlib.util
import itertools
import math
from pathlib import Path

import pytest

PATH = Path(__file__).parents[4] / "scripts/one-shot-tune-load-multisite.py"
SPEC = importlib.util.spec_from_file_location("one_shot_tune_multisite", PATH)
assert SPEC is not None and SPEC.loader is not None
tune = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tune)


def context():
    params = {"serviceSeconds": 1.0, "utilization": 1.0}
    return {
        "cloud": "cloud",
        "flowQuantum": 0.05,
        "parameters": {
            node: {"one-shot-sort": params, "one-shot-mixed": params}
            for node in ("edge-1", "edge-2", "cloud")
        },
        "initialReplicas": {
            node: {"one-shot-sort": 10, "one-shot-mixed": 10}
            for node in ("edge-1", "edge-2", "cloud")
        },
    }


def design():
    return {
        "baseFactors": {"edge-1": 0.25, "edge-2": 0.25},
        "profile": {"widthIntervals": 4},
        "hotspots": [{"edge": "edge-1", "function": "one-shot-sort", "center": 4}],
    }


def test_a_hotspot_ramps_geometrically_to_its_peak_and_back():
    sort = [
        tune.ramp_rates(context(), design(), i, 2)["edge-1"]["one-shot-sort"]
        for i in range(9)
    ]
    # Capacity 10/s x weight 1.0: base 2.5/s, peak 2 x 1.5 = 3 -> 30/s.
    assert sort[0] == sort[8] == 2.5
    assert sort[4] == 30
    assert sort == sort[::-1]
    ratios = [b / a for a, b in itertools.pairwise(sort[:5])]
    assert max(ratios) - min(ratios) < 0.05
    assert math.isclose(ratios[0], 12**0.25, rel_tol=0.02)
    # Untargeted edges stay at their base; the cloud's direct load grows slowly.
    rates = tune.ramp_rates(context(), design(), 4, 2)
    assert rates["edge-2"]["one-shot-sort"] == 2.5
    assert rates["cloud"]["one-shot-sort"] == pytest.approx(0.35)


def test_step_ratio_is_per_function_and_ignores_near_idle_rounding():
    windows = [
        {"edge-1": {"a": 0.05, "b": 1.0}},
        {"edge-1": {"a": 0.1, "b": 1.5}},
    ]
    assert tune.step_ratios(windows, ["edge-1"]) == {
        "edge-1/a": 1.0,
        "edge-1/b": 1.5,
    }


def test_intervals_are_named_after_the_hotspots_peaking_in_them():
    spots = {"hotspots": [{"edge": "edge-5", "center": 8.5}]}
    assert tune.ramp_label(spots, 8) == "i8-edge-5"
    assert tune.ramp_label(spots, 9) == "i9-edge-5"
    assert tune.ramp_label(spots, 10) == "i10-between"


def test_copied_nodes_take_their_source_cpus_on_their_own_site():
    scenario = {
        "oneShot": {
            "nodes": [
                {"id": "edge-1", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "hostCpuPlacement": {
                "nodes": {
                    "edge-1": {"cpus": [0], "maxFrequencyKHz": 1},
                    "cloud": {"cpus": [1], "maxFrequencyKHz": 1},
                },
                "operatorCpus": [8],
            },
            "runtimeDistribution": {"path": "d"},
        }
    }
    result = tune.expand_scenario(
        scenario,
        {
            "mirror": {"edge-6": "edge-1"},
            "neighbors": {"edge-1": ["edge-6"], "edge-6": ["edge-1"]},
            "sites": {"a": ["edge-1"], "b": ["edge-6"]},
        },
    )["oneShot"]
    assert [node["id"] for node in result["nodes"]] == ["edge-1", "edge-6", "cloud"]
    placement = result["hostCpuPlacement"]
    assert placement["nodes"]["edge-6"] == placement["nodes"]["edge-1"]
    assert placement["hosts"] == {"edge-1": "a", "edge-6": "b"}
    assert "runtimeDistribution" not in result


def test_a_throughput_cloud_holds_its_replicas_and_keeps_its_direct_load():
    limits = {"f": {"memoryMiB": 64, "maxReplicas": 9}, "g": {"memoryMiB": 128}}
    scenario = {
        "oneShot": {
            "nodes": [
                {"id": "edge-1", "kind": "edge"},
                {
                    "id": "cloud",
                    "kind": "cloud",
                    "memoryMiB": 4096,
                    "memoryCapacityMiB": 1344,
                    "functionLimits": limits,
                },
            ]
        }
    }
    sizing = {
        "mirror": {},
        "neighbors": {},
        "sites": {},
        "cloud": {"replicas": 27, "directReplicas": 7},
    }
    cloud = tune.expand_scenario(scenario, sizing)["oneShot"]["nodes"][1]
    assert cloud["memoryCapacityMiB"] == 27 * 192
    assert cloud["memoryMiB"] == 27 * 192 + tune.CLOUD_HEADROOM_MIB
    assert [limit["maxReplicas"] for limit in cloud["functionLimits"].values()] == [
        81,
        40,
    ]
    resized = tune.expand_observations(
        {
            "settings": {"nodes": [{"id": "cloud", "memoryMiB": 4096}]},
            "service": {},
            "measuredReplicaCountsPerFunction": {"cloud": 7},
        },
        sizing,
        {"oneShot": {"nodes": [cloud]}},
    )
    assert resized["settings"]["nodes"][0]["memoryMiB"] == cloud["memoryMiB"]
    assert resized["resizedCloud"]["replicas"] == 27
    # The cloud's direct rate stays that of its reference size.
    rates = tune.ramp_rates(context(), {**design(), "cloud": sizing["cloud"]}, 0, 2)
    assert rates["cloud"]["one-shot-sort"] == pytest.approx(0.2)


def test_copied_nodes_take_every_per_node_observation_of_their_source():
    """The observation reads each node's service and measured replica count."""
    observations = {
        "settings": {"nodes": [{"id": "edge-1", "memoryMiB": 2048}]},
        "service": {"edge-1": {"f": {"meanSeconds": 1.0}}},
        "measuredReplicaCountsPerFunction": {"edge-1": 1, "cloud": 7},
    }
    result = tune.expand_observations(
        observations, {"mirror": {"edge-6": "edge-1"}}, {"oneShot": {"nodes": []}}
    )
    assert result["service"]["edge-6"] == observations["service"]["edge-1"]
    assert result["measuredReplicaCountsPerFunction"] == {
        "edge-1": 1,
        "cloud": 7,
        "edge-6": 1,
    }
    assert [node["id"] for node in result["settings"]["nodes"]] == [
        "edge-1",
        "edge-6",
    ]
    assert "edge-6" not in observations["measuredReplicaCountsPerFunction"]
