"""Compose frozen soak resources independently of source preparation and plans."""

import re
from pathlib import Path
from typing import Any, Protocol, cast

from sonata_engine import JournalConfig, Resource, Task, Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor

from nanolab.tasks.compose import DockerComposeProject, isolated_compose_resource
from nanolab.tasks.platform import PlatformRequest, add_platform
from nanolab.tasks.soak.owned_functions import journaled_function_resource
from nanolab.tasks.soak.retention import CleanupState, journaled_compose_resource


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
