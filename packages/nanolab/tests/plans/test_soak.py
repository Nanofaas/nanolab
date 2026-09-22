import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sonata_engine import Resource, Task, TaskOutcome
from sonata_tasks.execution.bindings import RoleBindings

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.soak import (
    build_soak_plan,
    compose_frozen_soak_workflow,
)
from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.platform import PlatformFunction, PlatformRequest
from nanolab.tasks.soak.containerd_runtime import (
    BuildExecutionRecorder,
    ContainerdSoakRun,
)


def test_constructor_compiles_deferred_pipeline_without_side_effects(tmp_path):
    workflow = build_soak_plan(
        SimpleNamespace(workflow="soak", soak=object()),  # pyright: ignore[reportArgumentType]
        SimpleNamespace(provider="local"),  # pyright: ignore[reportArgumentType]
        RoleBindings({"host": _CompileOnlyExecutor()}),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
        tool_root=tmp_path,
    )
    titles = [task.task.title for task in workflow.compile().tasks]
    measure = next(
        i for i, title in enumerate(titles) if "single-version soak" in title
    )
    # Both are acquired before the measurement and released after it:
    # preparation pushes application images into the registry, and the helper
    # build both pushes into it and builds through the builder.
    before, after = titles[:measure], titles[measure + 1 :]
    assert any("Acquire local registry" in title for title in before)
    assert any("buildx builder" in title for title in before)
    assert any("Release local registry" in title for title in after)
    assert any("buildx builder" in title for title in after)
    assert not (tmp_path / "run").exists()


def test_compiled_platform_resources_outlive_measurement(tmp_path, monkeypatch):
    import nanolab.tasks.soak.teardown as teardown

    cleanup = {}

    def cleanup_commands(run_dir, timeout_s, artifact_limit):
        cleanup.update(
            run_dir=run_dir,
            timeout_s=timeout_s,
            artifact_limit=artifact_limit,
        )
        return object()

    monkeypatch.setattr(teardown, "LocalCleanupCommands", cleanup_commands)
    digest = "registry/image@sha256:" + "a" * 64
    request = PlatformRequest(
        backend="container",
        functions=(PlatformFunction("fn", digest, "{}", ()),),
        build_images=False,
        build_control_plane=False,
        control_plane_image=digest,
    )

    class Measure(Task):
        title = "Measure and report"
        observer = object()

        def stop_observer(self):
            pass

        def run(self, inputs):
            return TaskOutcome(value=None)

    ownership = Resource(
        title="Own endpoint",
        acquire=lambda inputs: None,
        release=lambda inputs, value: None,
    )
    workflow = compose_frozen_soak_workflow(
        request,
        RoleBindings({"host": _CompileOnlyExecutor()}),
        project=DockerComposeProject(
            "unique-soak",
            tmp_path / "compose.yml",
            "http://127.0.0.1:18080",
            build=False,
        ),
        measurement=Measure(),
        ownership=ownership,
        cwd=tmp_path,
        api_endpoint="http://127.0.0.1:18080",
        release_timeout_s=75.0,
    )
    tasks = workflow.compile().tasks
    consumer = next(
        index
        for index, task in enumerate(tasks)
        if task.task.title == "Measure and report"
    )
    assert all(
        index > consumer for index, task in enumerate(tasks) if task.kind == "release"
    )
    assert len(tasks[consumer].required_resources) >= 4
    assert not any("Build image" in task.task.title for task in tasks)
    assert cleanup["timeout_s"] == 75.0


class _CompileOnlyExecutor:
    def binding_key(self, role):
        return f"compile-only:{role}"

    def run(self, task, *, dry_run=False):
        raise AssertionError("plan construction must not execute commands")


def test_function_registration_uses_api_not_management_readiness(tmp_path, monkeypatch):
    import nanolab.plans.soak as module

    digest = "registry/image@sha256:" + "b" * 64
    request = PlatformRequest(
        backend="container",
        functions=(PlatformFunction("fn", digest, "{}", ()),),
        build_images=False,
        build_control_plane=False,
        control_plane_image=digest,
    )
    ownership = Resource(
        title="Own endpoint",
        acquire=lambda inputs: None,
        release=lambda inputs, value: None,
    )
    observed = {}

    def add_platform(workflow, request, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(resources=(), functions=(ownership,))

    monkeypatch.setattr(module, "add_platform", add_platform)
    monkeypatch.setattr(module, "isolated_compose_resource", lambda *a, **k: ownership)
    compose_frozen_soak_workflow(
        request,
        RoleBindings({"host": _CompileOnlyExecutor()}),
        project=DockerComposeProject(
            "owned",
            tmp_path / "compose.yaml",
            "http://127.0.0.1:18081/actuator/health/readiness",
            build=False,
        ),
        measurement=SimpleNamespace(observer=object()),  # pyright: ignore[reportArgumentType]
        ownership=ownership,
        cwd=tmp_path,
        api_endpoint="http://127.0.0.1:18080",
    )
    assert observed["local_endpoint"] == "http://127.0.0.1:18080"


def test_the_plan_acquires_a_builder_that_can_reach_the_local_registry(
    tmp_path, monkeypatch
):
    """Nothing else pins the driver option, and without it the run cannot publish.

    A `docker-container` builder runs buildkitd in a container of its own, so its
    `localhost` is itself: the build succeeds and the push into the registry this
    run just acquired does not. That failure lands after the build, so the option
    is pinned here rather than discovered there.
    """
    import nanolab.plans.soak as plan

    seen: list[dict] = []
    real = plan.buildx_builder_resource
    monkeypatch.setattr(
        plan,
        "buildx_builder_resource",
        lambda **kwargs: (seen.append(kwargs), real(**kwargs))[1],
    )

    build_soak_plan(
        SimpleNamespace(workflow="soak", soak=object()),  # pyright: ignore[reportArgumentType]
        SimpleNamespace(provider="local"),  # pyright: ignore[reportArgumentType]
        RoleBindings({"host": _CompileOnlyExecutor()}),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
        tool_root=tmp_path,
    )

    assert len(seen) == 1
    assert seen[0]["driver_options"] == ("network=host",)


def test_containerd_soak_plan_uses_stack_runtime_without_compose(tmp_path):
    scenario = (
        Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    )
    config = ScenarioConfig.model_validate(yaml.safe_load(scenario.read_text()))
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "multipass",
            "roles": {"stack": {"name": "owned-soak-vm"}},
            "containerdMavenRepository": "/tmp/test-containerd-maven",
        }
    )
    workflow = build_soak_plan(
        config,
        environment,
        RoleBindings({"host": _CompileOnlyExecutor(), "stack": _CompileOnlyExecutor()}),
        run_dir=tmp_path / "run",
        repo_root=Path(os.environ["NANOFAAS_ROOT"]),
        tool_root=Path(__file__).parents[4],
    )
    titles = [task.task.title for task in workflow.compile().tasks]
    assert any("rootless containerd test registry" in title for title in titles)
    assert any("rootless containerd test runtime" in title for title in titles)
    assert any("soak measurement" in title for title in titles)
    assert not any("Compose" in title or "Docker project" in title for title in titles)
    assert not (tmp_path / "run").exists()
    tasks = [item.task for item in workflow.compile().tasks]
    build = next(task for task in tasks if task.title == "Build control plane")
    measure = next(task for task in tasks if isinstance(task, ContainerdSoakRun))
    from sonata_tasks.command import CommandTask

    assert isinstance(build, CommandTask)
    assert isinstance(build.executor, BuildExecutionRecorder)
    assert build.executor is measure.executor
    assert not callable(build.argv)
    assert "-PcontainerdMavenLocal=true" in build.argv


def test_containerd_soak_rejects_native_until_binary_build_is_bound(tmp_path):
    scenario = (
        Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    )
    data = yaml.safe_load(scenario.read_text())
    data["soak"]["roles"]["control-plane"]["runtime"] = "native"
    data["soak"]["images"]["control-plane"]["variant"] = "native"
    config = ScenarioConfig.model_validate(data)
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "owned-soak-vm"}}}
    )
    with pytest.raises(ValueError, match="JVM process artifact"):
        build_soak_plan(
            config,
            environment,
            RoleBindings(
                {"host": _CompileOnlyExecutor(), "stack": _CompileOnlyExecutor()}
            ),
            run_dir=tmp_path / "run",
            repo_root=Path(os.environ["NANOFAAS_ROOT"]),
            tool_root=Path(__file__).parents[4],
        )


@pytest.mark.parametrize(
    ("role", "field", "value"),
    [
        ("word-stats-java", "variant", "native"),
        ("word-stats-javascript", "build_options", {"RUNTIME_IMAGE": "other"}),
        ("word-stats-java", "mode", "prebuilt"),
    ],
)
def test_containerd_soak_rejects_unapplied_image_recipe_before_acquisition(
    tmp_path, role, field, value
):
    scenario = (
        Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    )
    data = yaml.safe_load(scenario.read_text())
    data["soak"]["images"][role][field] = value
    if field == "mode":
        data["soak"]["images"][role]["digest"] = "registry/fn@sha256:" + "a" * 64
        data["soak"]["images"][role]["provenance_receipt"] = "receipt.json"
    config = ScenarioConfig.model_validate(data)
    environment = EnvironmentConfig.model_validate(
        {"provider": "local", "containerdMavenRepository": str(tmp_path / "maven")}
    )
    with pytest.raises(ValueError, match=r"unsupported.*recipe"):
        build_soak_plan(
            config,
            environment,
            RoleBindings(
                {"host": _CompileOnlyExecutor(), "stack": _CompileOnlyExecutor()}
            ),
            run_dir=tmp_path / "run",
            repo_root=Path(os.environ["NANOFAAS_ROOT"]),
            tool_root=Path(__file__).parents[4],
        )
    assert not (tmp_path / "run").exists()


def test_containerd_soak_refuses_unimplemented_p24_diagnostics(tmp_path):
    scenario = (
        Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    )
    config = ScenarioConfig.model_validate(yaml.safe_load(scenario.read_text()))
    assert config.soak is not None
    config.soak.prerequisites.required_coverage.append("sync")
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "owned-soak-vm"}}}
    )
    with pytest.raises(ValueError, match="prerequisite and diagnostic adapters"):
        build_soak_plan(
            config,
            environment,
            RoleBindings(
                {"host": _CompileOnlyExecutor(), "stack": _CompileOnlyExecutor()}
            ),
            run_dir=tmp_path / "run",
            repo_root=Path(os.environ["NANOFAAS_ROOT"]),
            tool_root=Path(__file__).parents[4],
        )


SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios-v2"


def _load_soak_scenario(name: str) -> ScenarioConfig:
    """Load a scenario file the way the CLI does, policy file and all."""
    from nanolab.cli.soak import load_soak_policy

    path = SCENARIOS / name
    resolved, receipt = load_soak_policy(yaml.safe_load(path.read_text()), path)
    assert receipt is not None
    return ScenarioConfig.model_validate(resolved)


def test_the_switch_soak_declares_both_modules_and_the_step():
    """The scenario issue #208 runs: one version, two strategies, one hour."""
    soak = _load_soak_scenario("memory-soak-scheduler-switch-container.yaml").soak
    assert soak is not None

    assert soak.purpose == "p24"
    # `soak` and not `advanced`: four of the five retained-population meters this
    # run's contract observes are registered only under the soak profile, and an
    # absent series is indistinguishable from a probe that never fired.
    assert soak.metrics_profile == "soak"
    # The campaign's criterion is a soak of at least sixty minutes.
    assert soak.phases.steady_s >= 60 * 60
    assert soak.scheduler_switch is not None
    assert soak.scheduler_switch.strategies == ["per-function", "shared-queue"]
    # Two strategies are indexed only when both queue modules are built in, and
    # the admin route is what the PATCH travels over.
    modules = soak.images["control-plane"].modules
    assert {"async-queue", "sync-queue", "runtime-config"} <= set(modules)
    assert (
        "scheduler_switch_duration_seconds_max"
        in soak.roles["control-plane"].required_metrics
    )


def test_the_switch_soak_declares_the_populations_the_soak_profile_publishes():
    """A population nothing declares is one no criterion can be held to.

    The five are registered by `SoakMetricsConfiguration` and the run's profile
    publishes them, but the sampler takes a metric's unit from the declaration
    and a `Criterion` may only name a metric its role declared required. Left
    out, they are readable in `samples.jsonl` with unit "unknown" and unusable by
    the contract this run is judged against.
    """
    from nanolab.tasks.soak.runtime import POPULATION_UNITS

    scenario = yaml.safe_load(
        (SCENARIOS / "memory-soak-scheduler-switch-container.yaml").read_text()
    )
    required = scenario["soak"]["roles"]["control-plane"]["required_metrics"]

    assert set(POPULATION_UNITS) <= set(required)


def test_the_switch_policy_keeps_the_shared_memory_contract():
    """The duplicated p24 criteria must not drift from the approved ones.

    A scenario cannot attach one more criterion to a shared policy file: the
    loader replaces the whole criteria list. So the switch soak restates the
    contract, and this is what stops the two copies disagreeing about a budget.
    """
    shared = yaml.safe_load((SCENARIOS / "memory-soak-policy.yaml").read_text())
    switch = yaml.safe_load(
        (SCENARIOS / "scheduler-switch-soak-policy.yaml").read_text()
    )
    by_id = {criterion["id"]: criterion for criterion in switch["criteria"]}

    for criterion in shared["criteria"]:
        assert by_id[criterion["id"]] == criterion
    assert set(by_id) - {c["id"] for c in shared["criteria"]} == {
        "control-plane.scheduler-switch-pause",
        "control-plane.scheduler-switch-pause-p99",
    }


def test_the_switch_pause_criterion_carries_the_frozen_millisecond_budget():
    """250 ms in the meter's unit, with the frozen number not raised."""
    switch = yaml.safe_load(
        (SCENARIOS / "scheduler-switch-soak-policy.yaml").read_text()
    )
    by_id = {criterion["id"]: criterion for criterion in switch["criteria"]}
    pause = by_id["control-plane.scheduler-switch-pause"]

    assert pause["metric"] == "scheduler_switch_duration_seconds_max"
    assert pause["unit"] == "seconds"
    assert pause["operation"] == "maximum"
    assert pause["threshold"] * 1000 == 250


def test_the_switch_p99_criterion_carries_the_frozen_budget_and_its_quantile():
    """100 ms on the bucket family, with the frozen number not raised.

    The two budgets are the same timer read two ways, so this pins both that the
    p99 is derived from the family rather than from a single series, and that its
    threshold is the frozen 100 ms in the unit Micrometer serves.
    """
    switch = yaml.safe_load(
        (SCENARIOS / "scheduler-switch-soak-policy.yaml").read_text()
    )
    by_id = {criterion["id"]: criterion for criterion in switch["criteria"]}
    p99 = by_id["control-plane.scheduler-switch-pause-p99"]

    assert p99["metric"] == "scheduler_switch_duration_seconds_bucket"
    assert p99["unit"] == "seconds"
    assert p99["operation"] == "percentile"
    assert p99["quantile"] == 0.99
    assert p99["threshold"] * 1000 == 100
    # Declared required for its role, without which no criterion may name it.
    scenario = yaml.safe_load(
        (SCENARIOS / "memory-soak-scheduler-switch-container.yaml").read_text()
    )
    roles = scenario["soak"]["roles"]
    for criterion in switch["criteria"]:
        assert criterion["metric"] in roles[criterion["role"]]["required_metrics"]
