from types import SimpleNamespace

from sonata_engine import Resource, Task, TaskOutcome
from sonata_tasks.execution.bindings import RoleBindings

from nanolab.plans.soak import (
    build_soak_plan,
    compose_frozen_soak_workflow,
)
from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.platform import PlatformFunction, PlatformRequest


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
    # The registry is acquired before the measurement and released after it:
    # preparation pushes application images and the helper build pushes the
    # helper, and both happen once the measuring task starts.
    assert any("Acquire local registry" in title for title in titles[:measure])
    assert any("Release local registry" in title for title in titles[measure + 1 :])
    # The builder is deliberately not acquired. A builder that can publish to a
    # local registry needs `--driver-opt network=host` when it is created, and
    # `buildx_builder_resource` passes no driver options, so acquiring it here
    # would create one that cannot push. It stays an operator precondition.
    assert not any("buildx builder" in title for title in titles)
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
