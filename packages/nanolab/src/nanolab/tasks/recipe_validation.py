"""Deploy and check images from a published NanoFaaS distribution."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.execution.models import CommandOptions, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.http import Endpoint, endpoint_argv

from nanolab.tasks.compose import (
    DestroyDockerCompose,
    DockerComposeProject,
    WaitForDockerCompose,
)
from nanolab.tasks.execution import ExecutionRole
from nanolab.tasks.http_function import HttpFunctionRegisterTask
from nanolab.tasks.recipe import RecipeDistribution

if TYPE_CHECKING:
    from nanolab.tasks.platform import PlatformFunction


class RecipeFunctionRegisterTask(Task[TaskResult]):
    """Register a function using the image recorded in the distribution."""

    def __init__(
        self,
        function: PlatformFunction,
        *,
        recipe_name: str,
        sdk: str,
        distribution: Resource[RecipeDistribution],
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        """Bind the selected recipe component to a NanoFaaS registration."""
        self.title = f"Register {function.name} from recipe"
        self.function = function
        self.recipe_name = recipe_name
        self.sdk = sdk
        self.distribution = distribution
        self.endpoint = endpoint
        self.executor = executor
        self.role: ExecutionRole = role
        self.cwd = cwd

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[TaskResult]:
        image = (
            inputs.resource(self.distribution)
            .function(self.recipe_name, self.sdk)
            .image.reference
        )
        manifest = replace(self.function.manifest(), image=image)
        return HttpFunctionRegisterTask(
            manifest,
            endpoint=self.endpoint,
            executor=self.executor,
            role=self.role,
            cwd=self.cwd,
        ).run(inputs)

    @override
    def _fingerprint_payload(self) -> object:
        return {
            "recipeName": self.recipe_name,
            "sdk": self.sdk,
            "function": self.function.name,
            "distribution": self.distribution.title,
        }


def recipe_compose_resource(
    project: DockerComposeProject,
    *,
    distribution: Resource[RecipeDistribution],
    executor: CommandTaskExecutor,
    cwd: Path,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[DockerComposeProject]:
    """Start Compose with the reported image, explicitly disabling builds."""

    def options(inputs: TaskInputs) -> CommandOptions:
        image = inputs.resource(distribution).control_plane().image.reference
        return CommandOptions(
            cwd=cwd,
            env={**project.env, "NANOFAAS_CONTROL_PLANE_IMAGE": image},
        )

    def down(inputs: TaskInputs) -> None:
        _ = DestroyDockerCompose(
            project,
            executor=executor,
            role=project.role,
            options=options(inputs),
            remove_volumes=True,
            remove_orphans=True,
        ).run(inputs)

    def acquire(inputs: TaskInputs) -> DockerComposeProject:
        down(inputs)
        _ = CommandTask(
            title=f"Deploy Docker Compose project {project.name} from recipe",
            argv=(
                "docker",
                "compose",
                "-f",
                str(project.file),
                "-p",
                project.name,
                "up",
                "-d",
                "--no-build",
                "--wait",
            ),
            executor=executor,
            role=project.role,
            options=options(inputs),
        ).run(inputs)
        _ = WaitForDockerCompose(
            project, executor=executor, role=project.role, options=options(inputs)
        ).run(inputs)
        return project

    return compensated_resource(
        title=f"Acquire Docker Compose project {project.name} from recipe",
        acquire=acquire,
        compensate=down,
        requires=(*requires, distribution),
    )


class RecipeMetadataCheckTask(Task[None]):
    """Verify build metadata from the running control plane."""

    def __init__(
        self,
        distribution: Resource[RecipeDistribution],
        *,
        executor: CommandTaskExecutor,
        run_dir: Path,
        endpoint: Endpoint,
        role: ExecutionRole = "host",
    ) -> None:
        """Store the distribution and endpoint for the runtime probe."""
        self.title = "Verify recipe build metadata"
        self.distribution = distribution
        self.executor = executor
        self.run_dir = run_dir
        self.endpoint = endpoint
        self.role = role

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        expected = inputs.resource(self.distribution)
        response = (
            CommandTask(
                title=self.title,
                argv=endpoint_argv(
                    self.endpoint,
                    lambda url: ("curl", "-fsS", f"{url}/modules/build-metadata"),
                ),
                executor=self.executor,
                role=self.role,
                semantic_key=f"recipe-metadata:{self.distribution.title}",
            )
            .run(inputs)
            .value
        )
        if response is None:
            raise RuntimeError("build metadata returned no response")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "build-metadata.json").write_text(response.stdout)
        try:
            data = json.loads(response.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Invalid build metadata response") from error
        build = data.get("build", {})
        component = expected.control_plane()
        if (
            sorted(data.get("modules", [])) != sorted(expected.modules)
            or build.get("type") != component.mode
            or build.get("variant") != component.variant
            or build.get("optimization") != component.optimization
        ):
            raise RuntimeError(
                "Running control-plane build metadata differs from recipe"
            )
        if expected.source is not None:
            for key in ("revision", "dirty"):
                value = expected.source.get(key)
                if value is not None and data.get(key) != value:
                    raise RuntimeError(
                        f"Running control-plane {key} differs from recipe"
                    )
        return TaskOutcome()

    @override
    def _fingerprint_payload(self) -> object:
        return {"distribution": self.distribution.title, "endpoint": self.endpoint}


class RecipeImageCheckTask(Task[None]):
    """Compare a running container's image ID with the distribution report."""

    def __init__(
        self,
        distribution: Resource[RecipeDistribution],
        *,
        executor: CommandTaskExecutor,
        run_dir: Path,
        project: DockerComposeProject,
        cwd: Path,
        function: tuple[str, str, str] | None = None,
    ) -> None:
        """Store the distribution and container to inspect."""
        self.title = (
            "Verify recipe control-plane image"
            if function is None
            else f"Verify recipe image of {function[0]}"
        )
        self.distribution = distribution
        self.executor = executor
        self.run_dir = run_dir
        self.project = project
        self.cwd = cwd
        self.function = function

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        expected = inputs.resource(self.distribution)
        if self.function is None:
            component = expected.control_plane()
            result = (
                CommandTask(
                    title="Locate recipe control plane",
                    argv=(
                        "docker",
                        "compose",
                        "-f",
                        str(self.project.file),
                        "-p",
                        self.project.name,
                        "ps",
                        "-q",
                        "control-plane",
                    ),
                    executor=self.executor,
                    role="host",
                    options=CommandOptions(cwd=self.cwd),
                )
                .run(inputs)
                .value
            )
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "compose-ps-control-plane.json").write_text(
                json.dumps({"stdout": result.stdout if result else None}) + "\n"
            )
            container = result.stdout.strip() if result else ""
        else:
            registration_name, recipe_name, sdk = self.function
            component = expected.function(recipe_name, sdk)
            container = f"nanofaas-{registration_name}-r1"
        if not container:
            raise RuntimeError("Recipe container was not found")
        result = (
            CommandTask(
                title=f"Inspect image of {container}",
                argv=("docker", "inspect", "--format", "{{.Image}}", container),
                executor=self.executor,
                role="host",
            )
            .run(inputs)
            .value
        )
        actual = result.stdout.strip() if result else ""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / f"image-{component.kind}-{component.name}.json").write_text(
            json.dumps(
                {
                    "container": container,
                    "imageId": actual,
                    "reference": component.image.reference,
                }
            )
            + "\n"
        )
        if actual != component.image.id:
            raise RuntimeError(
                f"Running image of {container} is {actual!r}, "
                f"distribution says {component.image.id!r}"
            )
        return TaskOutcome()

    @override
    def _fingerprint_payload(self) -> object:
        return {"distribution": self.distribution.title, "function": self.function}
