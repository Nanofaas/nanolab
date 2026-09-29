"""Compare a containerd function with the image selected by its recipe."""

from __future__ import annotations

import json
from pathlib import Path

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.containerd import (
    ContainerdImageInspectTask,
    require_containerd_image_identity,
)
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor

from nanolab.tasks.containerd_rootless import RootlessRun
from nanolab.tasks.execution import ExecutionRole
from nanolab.tasks.recipe import RecipeDistribution


class RecipeContainerdImageCheckTask(Task[None]):
    """Compare the owned container and its digest with distribution.json."""

    def __init__(
        self,
        distribution: Resource[RecipeDistribution],
        *,
        function: tuple[str, str, str],
        run: RootlessRun,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        run_dir: Path,
        cwd: Path | None = None,
    ) -> None:
        """Capture the run-owned container and expected recipe component."""
        self.title = f"Verify recipe image of {function[0]} in containerd"
        self.distribution = distribution
        self.function = function
        self.run_config = run
        self.executor = executor
        self.role = role
        self.run_dir = run_dir
        self.cwd = cwd

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Save runtime evidence, then compare it with the recipe report."""
        registration_name, recipe_name, sdk = self.function
        expected = inputs.resource(self.distribution).function(recipe_name, sdk).image
        reference = expected.reference
        identity = (
            ContainerdImageInspectTask(
                container_argv=(
                    "bash",
                    str(self.run_config.script),
                    "inspect-owned",
                    self.run_config.run_id,
                    str(self.run_config.repo_root),
                    registration_name,
                    "1",
                ),
                image_argv=(
                    "bash",
                    str(self.run_config.script),
                    "image-inspect",
                    self.run_config.run_id,
                    str(self.run_config.repo_root),
                    reference,
                ),
                executor=self.executor,
                role=self.role,
                options=CommandOptions(cwd=self.cwd),
            )
            .run(inputs)
            .value
        )
        if identity is None:
            raise RuntimeError("containerd image inspection returned no identity")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / f"containerd-{registration_name}-image.json").write_text(
            json.dumps(identity.container, sort_keys=True, indent=2) + "\n"
        )
        (
            self.run_dir / f"containerd-{registration_name}-image-identity.json"
        ).write_text(json.dumps(identity.image, sort_keys=True, indent=2) + "\n")
        if expected.digest is None:
            raise RuntimeError("Recipe image has no published digest")
        require_containerd_image_identity(
            identity, reference=reference, digest=expected.digest
        )
        return TaskOutcome(value=None)

    def _fingerprint_payload(self) -> object:
        return {
            "distribution": self.distribution.title,
            "function": self.function,
            "runId": self.run_config.run_id,
            "bindingKey": self.executor.binding_key(self.role),
        }
