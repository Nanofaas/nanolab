"""Reject heap-analysis configs that stray from a control-plane, two-dump scope."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from nanolab.config.heap_analysis import HeapAnalysisConfig
from nanolab.config.scenario import ScenarioConfig


def valid_heap_analysis() -> dict:
    return {
        "target": "control-plane",
        "warmup_s": 30,
        "steady_s": 120,
        "drain_s": 60,
        "roles": {
            "control-plane": {
                "runtime": "jvm",
                "expected_cpu": 2.0,
                "memory_limit_bytes": 1073741824,
                "required_metrics": [
                    "process_rss_bytes",
                    "cgroup_memory_usage_bytes",
                ],
                "required_capabilities": ["rss", "cgroup"],
                "runtime_options": ["-Xmx512m"],
                "collection_sources": ["procfs", "cgroup"],
            },
            "word-stats-java": {
                "runtime": "jvm",
                "expected_cpu": 2.0,
                "memory_limit_bytes": 1073741824,
                "required_metrics": [
                    "process_rss_bytes",
                    "cgroup_memory_usage_bytes",
                ],
                "required_capabilities": ["rss", "cgroup"],
                "runtime_options": [],
                "collection_sources": ["procfs", "cgroup"],
            },
            "word-stats-javascript": {
                "runtime": "node",
                "expected_cpu": 1.0,
                "memory_limit_bytes": 536870912,
                "required_metrics": [
                    "process_rss_bytes",
                    "cgroup_memory_usage_bytes",
                ],
                "required_capabilities": ["rss", "cgroup"],
                "runtime_options": [],
                "collection_sources": ["procfs", "cgroup"],
            },
        },
        "images": {
            "control-plane": {
                "variant": "jvm",
                "platform": "linux/amd64",
            },
            "word-stats-java": {
                "variant": "jvm",
                "platform": "linux/amd64",
            },
            "word-stats-javascript": {
                "variant": "default",
                "platform": "linux/amd64",
            },
        },
        "workload": {
            "rates": {"word-stats-java": 100, "word-stats-javascript": 100},
            "preallocated_vus": 200,
            "max_vus": 200,
            "max_error_ratio": 0,
            "max_dropped_iterations": 0,
        },
        "max_dumps": 2,
        "max_dump_bytes": 2147483648,
        "artifact_limit_bytes": 4294967296,
        "diagnostic_timeout_s": 60,
        "mat_memory_mib": 2048,
        "mat_cpus": 2.0,
        "mat_timeout_s": 300,
    }


def test_control_plane_heap_analysis_is_valid():
    config = HeapAnalysisConfig.model_validate(valid_heap_analysis())
    assert config.target == "control-plane"
    assert config.workload.rates == {
        "word-stats-java": 100,
        "word-stats-javascript": 100,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("target", "word-stats-java", "target must be control-plane"),
        ("max_dumps", 3, "exactly two dumps"),
    ],
)
def test_heap_analysis_rejects_unsupported_scope(field, value, message):
    raw = valid_heap_analysis()
    raw[field] = value
    with pytest.raises(ValidationError, match=message):
        HeapAnalysisConfig.model_validate(raw)


@pytest.mark.parametrize(
    "rates",
    [
        {"word-stats-java": 100},
        {
            "word-stats-java": 100,
            "word-stats-javascript": 100,
            "word-stats-native": 100,
        },
    ],
)
def test_heap_analysis_rejects_wrong_function_count(rates):
    raw = valid_heap_analysis()
    raw["workload"]["rates"] = rates
    with pytest.raises(ValidationError, match="exactly two functions"):
        HeapAnalysisConfig.model_validate(raw)


def test_heap_analysis_rejects_non_jvm_control_plane_role():
    raw = valid_heap_analysis()
    raw["roles"]["control-plane"]["runtime"] = "native"
    with pytest.raises(ValidationError, match="jvm runtime"):
        HeapAnalysisConfig.model_validate(raw)


def test_heap_analysis_rejects_mismatched_role_and_image_keys():
    raw = valid_heap_analysis()
    raw["images"]["unrelated"] = copy.deepcopy(raw["images"]["control-plane"])
    with pytest.raises(ValidationError, match="same set of keys"):
        HeapAnalysisConfig.model_validate(raw)


def _scenario_data() -> dict:
    return {
        "workflow": "heap-analysis",
        "backend": "container",
        "functions": ["word-stats-java", "word-stats-javascript"],
        "heapAnalysis": valid_heap_analysis(),
    }


def test_heap_analysis_scenario_is_valid():
    config = ScenarioConfig.model_validate(_scenario_data())
    assert config.heap_analysis is not None
    assert config.heap_analysis.roles["control-plane"].runtime == "jvm"


def test_heap_analysis_scenario_requires_container_backend():
    data = _scenario_data()
    data["backend"] = "k8s"
    with pytest.raises(ValidationError, match="container backend"):
        ScenarioConfig.model_validate(data)


def test_heap_analysis_scenario_requires_heap_analysis_block():
    data = _scenario_data()
    del data["heapAnalysis"]
    with pytest.raises(ValidationError, match="requires its heapAnalysis block"):
        ScenarioConfig.model_validate(data)


def test_heap_analysis_scenario_requires_jvm_control_plane_role():
    data = _scenario_data()
    data["heapAnalysis"]["roles"]["control-plane"]["runtime"] = "native"
    with pytest.raises(ValidationError, match="jvm runtime"):
        ScenarioConfig.model_validate(data)


def test_heap_analysis_scenario_resources_limited_to_functions_and_control_plane():
    data = _scenario_data()
    data["resources"] = {"unrelated-function": {"limits": {"cpu": 1.0}}}
    with pytest.raises(ValidationError, match="selected functions or control-plane"):
        ScenarioConfig.model_validate(data)


def test_heap_analysis_scenario_accepts_control_plane_and_function_resources():
    data = _scenario_data()
    data["resources"] = {
        "control-plane": {"limits": {"cpu": 2.0}},
        "word-stats-java": {"limits": {"cpu": 2.0}},
    }
    config = ScenarioConfig.model_validate(data)
    limits = config.resources["control-plane"].limits
    assert limits is not None
    assert limits.cpu == 2.0


def test_heap_analysis_scenario_rejects_resources_that_contradict_the_role():
    """An incoherent scenario must fail here, not twenty minutes into a run."""
    data = _scenario_data()
    data["resources"] = {"word-stats-java": {"limits": {"cpu": 1.0}}}
    with pytest.raises(ValidationError, match="disagree with the heap-analysis role"):
        ScenarioConfig.model_validate(data)


def test_heap_analysis_scenario_rejects_functions_the_workload_never_drives():
    data = _scenario_data()
    data["functions"] = ["word-stats-java"]
    with pytest.raises(ValidationError, match="cover exactly the selected functions"):
        ScenarioConfig.model_validate(data)


def test_heap_analysis_rejects_workload_rates_without_a_role():
    """The branch's own CLI fixture shipped exactly this shape as 'fully valid'."""
    raw = valid_heap_analysis()
    del raw["roles"]["word-stats-javascript"]
    del raw["images"]["word-stats-javascript"]
    with pytest.raises(ValidationError, match="cover exactly the non-control-plane"):
        HeapAnalysisConfig.model_validate(raw)


def test_non_heap_workflow_rejects_heap_analysis_block():
    data = {
        "workflow": "loadtest",
        "backend": "k8s",
        "functions": ["word-stats-java"],
        "heapAnalysis": valid_heap_analysis(),
    }
    with pytest.raises(ValidationError, match="belongs only to the heap-analysis"):
        ScenarioConfig.model_validate(data)


def test_heap_analysis_rejects_a_scenario_pinned_helper_image():
    """The helper is built per run, so a scenario may not name one at all.

    A digest written into a scenario names bytes in whichever registry built
    them; accepting one here would hand other machines an unpullable reference.
    """
    raw = valid_heap_analysis()
    raw["helper_image"] = "nanolab/heap-helper@sha256:" + "a" * 64
    with pytest.raises(ValidationError, match="helper_image"):
        HeapAnalysisConfig.model_validate(raw)
