from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
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


def _declared_sections():
    """Return the sections this scenario declares relevant, as its policy has them."""
    return {
        "images": {
            "control-plane": {"mode": "build", "variant": "jvm"},
            "word-stats-java": {"mode": "build", "variant": "jvm"},
            "word-stats-javascript": {"mode": "build", "variant": "default"},
        },
        "roles": {"control-plane": {"runtime": "jvm", "runtime_options": []}},
        "retention_s": {
            "unkeyed-sync-outcome": 30,
            "terminal-key-and-readable-outcome": 300,
            "live-key-and-execution": 1800,
        },
        "workload": {"rates": {"word-stats-java": 100}, "preallocated_vus": 200},
    }


_SDK_IMAGES = {
    "control-plane": _DIGEST,
    "word-stats-java": "sha256:" + "b" * 64,
    "word-stats-javascript": "sha256:" + "c" * 64,
}


def _freeze_fixture(tmp_path, coverage):
    """Return a prepared build whose requested coverage is exactly `coverage`."""
    import nanolab.tasks.soak.runtime as runtime

    writer = ArtifactWriter(tmp_path, 8 * 1024 * 1024)
    declared = _declared_sections()
    config = SimpleNamespace(
        metrics_profile="soak",
        prerequisites=SimpleNamespace(
            required_coverage=list(coverage),
            relevant_config_keys={name: list(declared) for name in coverage},
        ),
        model_dump=lambda *, mode: dict(declared),
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
        images=dict(_SDK_IMAGES),
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
    return runtime._freeze_prerequisite_inputs(prepared), prepared, declared, writer


def test_builtin_prerequisite_inputs_are_derived_from_frozen_preparation(tmp_path):
    frozen, prepared, declared, writer = _freeze_fixture(tmp_path, ["sync"])

    assert frozen["images"] == prepared.images
    assert set(frozen["relevant_config"]) == {"sync"}
    assert frozen["relevant_config"]["sync"]["role"] == "word-stats-java"
    assert frozen["relevant_config"]["sync"]["request"] == {"input": {"text": "hello"}}
    # Exactly the declared projections, which is what the acceptance gate reads.
    for key, section in declared.items():
        assert frozen["relevant_config"]["sync"][key] == section
    assert frozen["settlement"]["control-plane"]["outcomes"] == {
        "limit": 0,
        "retention_s": 35,
    }
    _inputs(frozen, frozenset({"sync"}))
    writer.close()


@pytest.mark.parametrize(
    "coverage",
    ["sync", "idempotent-replay", "function-name-churn"],
)
def test_every_derivable_profile_is_emitted_for_its_own_coverage(tmp_path, coverage):
    """Each derivation the freeze claims is emitted, not just the first.

    Removing any one branch of `_freeze_prerequisite_inputs` leaves this failing
    with `relevant config must cover exactly the required profiles`, which is the
    check that used to be a `coverage != {"sync"}` refusal instead.
    """
    frozen, prepared, declared, writer = _freeze_fixture(tmp_path, [coverage])

    assert set(frozen["relevant_config"]) == {coverage}
    profile = frozen["relevant_config"][coverage]
    assert profile["role"] == profile["function"] == "word-stats-java"
    assert profile["expected_output"] == {"wordCount": 1}
    for key, section in declared.items():
        assert profile[key] == section
    # The churned names must land on this run's own EXTERNAL SDK container: the
    # prerequisite factory rejects a spec that disagrees on any of these three,
    # so they are what the profile has to carry and no recipe can invent.
    if coverage == "function-name-churn":
        assert profile["function_spec"]["image"] == prepared.images["word-stats-java"]
        assert profile["function_spec"]["executionMode"] == "EXTERNAL"
        assert profile["function_spec"]["endpointUrl"] == (
            "http://function-1:8080/invoke"
        )
        assert frozen["settlement"]["control-plane"]["retired_owners"] == {
            "limit": 0,
            "retention_s": 0,
        }
    if coverage == "idempotent-replay":
        assert frozen["settlement"]["control-plane"]["idempotency_entries"] == {
            "limit": 0,
            "retention_s": 305,
        }
    _inputs(frozen, frozenset({coverage}))
    writer.close()


def test_a_coverage_with_no_builtin_injection_is_refused_before_any_lifetime(tmp_path):
    """The refusal stays cheap for the five profiles this machine cannot derive."""
    with pytest.raises(ValueError, match="no built-in injection exists for timeout"):
        _freeze_fixture(tmp_path, ["sync", "timeout"])


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
