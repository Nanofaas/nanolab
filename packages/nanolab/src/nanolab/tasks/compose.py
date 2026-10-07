"""Docker Compose resources that run an experiment in a fresh, isolated project."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sonata_engine import Resource
from sonata_tasks.compose import (
    DeployDockerCompose,
    DestroyDockerCompose,
    WaitForDockerCompose,
)
from sonata_tasks.compose import (
    DockerComposeProject as SharedDockerComposeProject,
)
from sonata_tasks.compose import (
    docker_compose_resource as shared_compose_resource,
)
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor


@dataclass(frozen=True, slots=True)
class DockerComposeProject(SharedDockerComposeProject):
    """A shared Compose project pinned to a role and carrying its own env."""

    role: str = "host"
    env: Mapping[str, str] = field(default_factory=dict)


def isolated_compose_resource(
    project: DockerComposeProject,
    *,
    executor: CommandTaskExecutor,
    cwd: Path | None = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[DockerComposeProject]:
    """Run the experiment in a fresh Compose project and remove all state."""
    return shared_compose_resource(
        project,
        executor=executor,
        role=project.role,
        options=CommandOptions(cwd=cwd, env=project.env),
        pre_clean=True,
        remove_volumes=True,
        remove_orphans=True,
        requires=requires,
    )


docker_compose_resource = isolated_compose_resource

__all__ = [
    "DeployDockerCompose",
    "DestroyDockerCompose",
    "DockerComposeProject",
    "WaitForDockerCompose",
    "docker_compose_resource",
    "isolated_compose_resource",
]
