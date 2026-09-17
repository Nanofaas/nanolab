from pathlib import Path
from types import SimpleNamespace
from typing import cast

import yaml
from sonata_tasks.execution.bindings import RoleBindings

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.soak import build_soak_plan
from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.prerequisites import _inputs

_DIGEST = "sha256:" + "a" * 64


class _CompileOnlyExecutor:
    def binding_key(self, role):
        return f"compile-only:{role}"

    def run(self, task, *, dry_run=False):
        raise AssertionError("plan construction must not execute commands")


def _soak_config():
    return SimpleNamespace(
        prerequisites=SimpleNamespace(mode="run", required_coverage=["sync"]),
        diagnostics=SimpleNamespace(operations={}, baseline_operations={}),
        artifact_limit_bytes=8 * 1024 * 1024 * 1024,
    )


def test_public_builder_declares_builtin_prerequisites_without_caller_options(
    tmp_path, monkeypatch
):
    import nanolab.tasks.soak.runtime as runtime

    tasks = []
    original = runtime.RunSingleVersionSoak

    def record(*args, **kwargs):
        task = original(*args, **kwargs)
        tasks.append(task)
        return task

    monkeypatch.setattr(runtime, "RunSingleVersionSoak", record)
    build_soak_plan(
        cast(
            ScenarioConfig,
            SimpleNamespace(workflow="soak", soak=_soak_config()),
        ),
        cast(EnvironmentConfig, SimpleNamespace(provider="local")),
        RoleBindings({"host": _CompileOnlyExecutor()}),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
        tool_root=tmp_path,
    )

    preparation = runtime._runtime_preparation_options(
        tasks[0].config.soak, tasks[0].options
    )
    assert preparation.prerequisite_provider_available is True


def test_builtin_prerequisite_inputs_are_derived_from_frozen_preparation(tmp_path):
    import nanolab.tasks.soak.runtime as runtime

    writer = ArtifactWriter(tmp_path, 8 * 1024 * 1024)
    config = SimpleNamespace(
        metrics_profile="soak",
        prerequisites=SimpleNamespace(required_coverage=["sync"]),
        retention_s={
            "unkeyed-sync-outcome": 30,
            "terminal-key-and-readable-outcome": 300,
            "live-key-and-execution": 1800,
        },
        roles={
            "control-plane": SimpleNamespace(runtime="jvm"),
            "word-stats-java": SimpleNamespace(runtime="jvm"),
            "word-stats-javascript": SimpleNamespace(runtime="node"),
        },
    )
    prepared = SimpleNamespace(
        config=config,
        images={
            "control-plane": _DIGEST,
            "word-stats-java": "sha256:" + "b" * 64,
            "word-stats-javascript": "sha256:" + "c" * 64,
        },
        payloads={
            "word-stats-java": [
                {"input": {"text": "hello"}, "expected": {"wordCount": 1}}
            ],
            "word-stats-javascript": [
                {"input": {"text": "hello"}, "expected": {"wordCount": 1}}
            ],
        },
        writer=writer,
    )

    frozen = runtime._freeze_prerequisite_inputs(prepared)

    assert frozen["images"] == prepared.images
    assert set(frozen["relevant_config"]) == {"sync"}
    assert frozen["relevant_config"]["sync"]["role"] == "word-stats-java"
    assert frozen["relevant_config"]["sync"]["request"] == {"input": {"text": "hello"}}
    assert frozen["settlement"]["control-plane"]["outcomes"] == {
        "limit": 0,
        "retention_s": 35,
    }
    _inputs(frozen, frozenset({"sync"}))
    writer.close()


def test_candidate_preset_is_runnable_at_two_hundred_requests_per_second():
    scenario = yaml.safe_load(
        (
            Path(__file__).parents[2]
            / "scenarios-v2/memory-soak-sync-candidate-diagnostic-container.yaml"
        ).read_text()
    )

    assert sum(scenario["soak"]["workload"]["rates"].values()) == 200
    assert scenario["soak"]["prerequisites"]["required_coverage"] == ["sync"]
    # The helper is built per run, so the scenario must name none: a digest
    # here would be unpullable on any machine but the one that built it.
    assert "helper_images" not in scenario["soak"]["diagnostics"]
    assert (
        "--require=/opt/nanolab/node-diagnostic-control.cjs"
        in scenario["soak"]["roles"]["word-stats-javascript"]["runtime_options"]
    )
    for role in scenario["soak"]["roles"].values():
        assert set(role["required_capabilities"]) == {"procfs", "docker-engine"}
    assert scenario["soak"]["diagnostics"]["gc_completion_evidence"] == {
        "control-plane": "jdk.GarbageCollection",
        "word-stats-java": "jdk.GarbageCollection",
        "word-stats-javascript": "node:perf_hooks:major-gc",
    }


def test_observed_collection_sources_are_derived_from_available_samples():
    import nanolab.tasks.soak.runtime as runtime

    rows = (
        SimpleNamespace(source="procfs", availability="observed"),
        SimpleNamespace(source="docker-engine/memory_stats", availability="observed"),
        SimpleNamespace(source="prometheus", availability="observed"),
        SimpleNamespace(source="ignored", availability="unavailable"),
    )

    assert runtime._observed_collection_sources(rows) == {
        "procfs",
        "docker-engine",
        "prometheus",
    }
