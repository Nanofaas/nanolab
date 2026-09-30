"""Deferred public single-version soak and frozen runtime composition."""

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, cast, override
from uuid import uuid4

from sonata_engine import JournalConfig, Resource, Task, Workflow
from sonata_tasks.buildx import buildx_builder_resource
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult
from sonata_tasks.registry import docker_registry_resource

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.compose import DockerComposeProject, isolated_compose_resource
from nanolab.tasks.deployment import REGISTRY_CONTAINER_NAME
from nanolab.tasks.platform import PlatformRequest, add_platform
from nanolab.tasks.soak.helper_build import HELPER_BUILDER
from nanolab.tasks.soak.owned_functions import (
    journaled_function_resource,
)
from nanolab.tasks.soak.retention import CleanupState, journaled_compose_resource

SOAK_BUILDKIT_IMAGE = (
    "moby/buildkit@sha256:"
    "1e110c71d389d6d24f67b9438e2f7b8da749a6ff407b22a1631e025c95599368"
)


class _RecipeBuilderExecutor(RoleBoundCommandTaskExecutor):
    """Keep Sonata builder ownership without changing global builder selection."""

    @override
    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        if task.argv[:3] == ("docker", "buildx", "create"):
            task = replace(task, argv=tuple(arg for arg in task.argv if arg != "--use"))
        return super().run(task, dry_run=dry_run)


class _MeasurementControl(Protocol):
    observer: Any

    def stop_observer(self) -> None: ...


def compose_frozen_soak_workflow(
    request: PlatformRequest,
    bindings: RoleBindings,
    *,
    project: DockerComposeProject,
    measurement: Task[Any],
    api_endpoint: str,
    ownership: Resource[Any],
    cwd: Path,
    workflow_id: str = "soak",
    release_timeout_s: float = 60.0,
) -> Workflow:
    """Compose an already frozen deployment using the real platform resources.

    The caller must supply a run-owned Compose file using the frozen control
    plane digest and effective limits, and an exclusive endpoint lease resource.
    This lower-level entry does not perform source preparation or certify it.
    """
    if request.backend != "container" or project.build:
        raise ValueError("soak requires container Compose with builds disabled")
    if (
        request.build_images
        or request.build_control_plane
        or request.push_function_images
    ):
        raise ValueError("no image build or publication is allowed after freeze")
    references = [
        request.control_plane_image,
        *(item.image for item in request.functions),
    ]
    if any(
        not isinstance(ref, str)
        or re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", ref) is None
        for ref in references
    ):
        raise ValueError("every deployed application image requires a frozen digest")
    executor = RoleBoundCommandTaskExecutor(bindings)
    from nanolab.tasks.soak.runtime import capture_function_owner, verify_function_owner

    project_file = project.file if project.file.is_absolute() else cwd / project.file
    cleanup_state = CleanupState(
        JournalConfig(path=project_file.absolute().parent / "cleanup.jsonl")
    )
    from nanolab.tasks.soak.teardown import LocalCleanupCommands

    cleanup_command = LocalCleanupCommands(
        project_file.absolute().parent,
        timeout_s=release_timeout_s,
        artifact_limit=8 * 1024 * 1024,
    )
    workflow = Workflow(workflow_id=workflow_id)
    compose = isolated_compose_resource(
        project,
        executor=executor,
        cwd=cwd,
        requires=(ownership,),
    )
    compose = journaled_compose_resource(
        compose,
        project=project,
        cwd=cwd,
        cleanup_state=cleanup_state,
        command=cleanup_command,
    )
    platform = add_platform(
        workflow,
        request,
        executor=executor,
        cwd=cwd,
        control_plane_process=lambda: compose,
        local_endpoint=api_endpoint,
        requires=(ownership,),
    )
    # A kept soak owns a unique platform, so its functions remain available for
    # investigation without changing other workflows' delete-on-keep default.
    functions = tuple(
        journaled_function_resource(
            resource,
            ownership=lambda function=function: capture_function_owner(
                function.name,
                api_endpoint,
                request.control_plane_image,
                project.name,
                str(cwd.absolute()),
            ),
            cleanup_state=cleanup_state,
            verify_owner=verify_function_owner,
        )
        for function, resource in zip(
            request.functions, platform.functions, strict=True
        )
    )
    observer = Resource(
        title="Own continuous soak observer",
        acquire=lambda inputs: cast(_MeasurementControl, measurement).observer,
        release=lambda inputs, value: cast(
            _MeasurementControl, measurement
        ).stop_observer(),
        requires=(*platform.resources, *functions),
        always_release=True,
    )
    workflow.add(
        measurement,
        requires=(ownership, *platform.resources, *functions, observer),
    )
    return workflow


def build_soak_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    run_dir: Path,
    repo_root: Path,
    tool_root: Path,
    runtime_options: Any = None,
) -> Workflow:
    """Compile deferred preparation/deployment without Docker or source I/O."""
    if config.workflow != "soak" or config.soak is None:
        raise ValueError("build_soak_plan requires a soak scenario")
    if getattr(config, "backend", "container") == "containerd":
        from nanolab.plans.containerd_soak import build_containerd_soak_plan

        return build_containerd_soak_plan(
            config,
            environment,
            bindings,
            run_dir=run_dir,
            repo_root=repo_root,
            tool_root=tool_root,
        )
    if environment.provider != "local":
        raise ValueError("soak currently requires a local environment")
    from nanolab.tasks.soak.runtime import RunSingleVersionSoak, RuntimeOptions

    workflow = Workflow(workflow_id="soak")
    recipe_profile = getattr(config, "recipe_profile", None)
    recipe_requires = ()
    actual_options = (
        runtime_options
        if runtime_options is not None
        else RuntimeOptions(allow_diagnostic_target_stop_on_cancel=True)
    )
    executor = RoleBoundCommandTaskExecutor(bindings)
    builder_config = None
    if recipe_profile is not None:
        from nanolab.tasks.soak.preparation import check_local_process_support
        from nanolab.tasks.soak.recipe import validate_soak_recipe

        validate_soak_recipe(
            recipe_profile,
            config.soak,
            platform=config.soak.images["control-plane"].platform,
        )
        preparation = actual_options.preparation
        if (
            preparation.recipe_profile not in {None, recipe_profile}
            or preparation.recipe_builder is not None
            or preparation.registry
            not in {"localhost:5000/nanofaas", "127.0.0.1:5000/nanofaas"}
            or actual_options.helper_builder != HELPER_BUILDER
            or preparation.build_observer is not None
            or actual_options.prepared is not None
        ):
            raise ValueError("Conflicting recipe soak runtime options")
        name = "nanolab-soak-recipe-" + uuid4().hex
        actual_options = replace(
            actual_options,
            helper_builder=name,
            preparation=replace(
                preparation,
                recipe_profile=recipe_profile,
                recipe_builder=name,
                registry="127.0.0.1:5000/nanofaas",
            ),
        )
        config_path = run_dir.absolute() / "soak-buildkitd.toml"

        soak = config.soak

        def preflight(_inputs):
            check_local_process_support(soak)
            run_dir.mkdir(parents=True, exist_ok=True)
            with config_path.open("x") as stream:
                stream.write('[registry."127.0.0.1:5000"]\n  http = true\n')
            return

        recipe_requires = (
            Resource(
                title="Check recipe soak daemon support",
                acquire=preflight,
                release=lambda inputs, value: None,
            ),
        )
        executor = _RecipeBuilderExecutor(bindings)
        builder_config = str(config_path)
    # Both are acquired before the task runs, because preparation and the helper
    # build push into the registry and build with the builder -- acquiring them
    # inside the frozen deployment's own resources would be too late.
    #
    # Each removes only what it created and leaves what it found running alone, so
    # an operator's own registry or builder is never torn down by a run. The
    # builder name comes from the same options the run builds with, so the two
    # cannot name different builders.
    builder = buildx_builder_resource(
        name=actual_options.helper_builder,
        executor=executor,
        role="host",
        # A `docker-container` builder has a `localhost` of its own, so without
        # this the push into the registry above cannot resolve.
        driver_options=(
            "network=host",
            *((f"image={SOAK_BUILDKIT_IMAGE}",) if recipe_profile is not None else ()),
        ),
        requires=recipe_requires,
        buildkitd_config=builder_config,
    )
    registry = docker_registry_resource(
        executor=RoleBoundCommandTaskExecutor(bindings),
        role="host",
        container=REGISTRY_CONTAINER_NAME,
        requires=recipe_requires,
    )
    workflow.add(
        RunSingleVersionSoak(
            config,
            bindings,
            run_dir=run_dir,
            repo_root=repo_root,
            tool_root=tool_root,
            # This public owned-soak caller authorizes cleanup of its own target.
            # Explicit integration options retain their default-deny permission.
            options=actual_options,
            recipe_builder=builder if recipe_profile is not None else None,
            keep=lambda: workflow.keep,
        ),
        requires=(registry, builder),
    )
    return workflow


def teardown_soak_run(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    run_dir: Path,
    repo_root: Path,
    tool_root: Path,
) -> Workflow:
    """Compile validated replay of this run's retained ownership records."""
    from nanolab.tasks.soak.teardown import build_teardown_workflow

    return build_teardown_workflow(
        config,
        environment,
        bindings,
        run_dir=run_dir,
        repo_root=repo_root,
        tool_root=tool_root,
    )
