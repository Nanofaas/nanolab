"""Own the buildx builder a helper build publishes through, for one run.

The pinned `buildx_builder_resource` does almost this, and cannot do it:
publishing to a local registry needs `network=host` on the builder's own
container, which is a `--driver-opt` given at creation, and that resource passes
no driver options. A `buildkitd_config` does not substitute for it -- measured:
`[worker.oci] networkMode = "host"` leaves the builder container on the bridge
network, because that key governs the network of build steps rather than the
daemon that pushes. So the create argv lives here, carrying the flag it exists
for, and the rest of the semantics are the pinned resource's: acquiring adopts a
builder that already exists rather than replacing it, and releasing removes only
what this resource created.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import best_effort
from sonata_tasks.execution.models import CommandOptions, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor

# The builder a run looks for when nothing overrides it, shared with the runtimes
# that build through it: the plan and the run must never name different builders,
# or the run builds with one nothing acquired.
HELPER_BUILDER = "nanolab-heap-analysis"


def _run(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: str,
    options: CommandOptions,
    *args: str,
    expected: frozenset[int] = frozenset({0}),
) -> TaskResult:
    """Run one docker buildx command and return what it reported."""
    outcome = CommandTask(
        title=f"docker buildx {' '.join(args)}",
        argv=("docker", "buildx", *args),
        executor=executor,
        role=role,
        options=replace(options, expected_exit_codes=expected),
    ).run(inputs)
    if outcome.value is None:
        raise RuntimeError("docker buildx returned no command result")
    return outcome.value


def helper_builder_resource(
    *,
    executor: CommandTaskExecutor,
    name: str = HELPER_BUILDER,
    role: str = "host",
    options: CommandOptions | None = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[str]:
    """Acquire this run's builder, creating one only when it is missing.

    Acquiring returns the builder name for one this resource created, or
    "existing" when it left a pre-existing builder untouched; releasing removes
    only the former, so an operator's own builder survives a run.
    """
    current = options or CommandOptions()

    def remove(inputs: TaskInputs) -> None:
        _ = _run(inputs, executor, role, current, "rm", "--force", name)

    def bootstrap(inputs: TaskInputs) -> None:
        try:
            _ = _run(
                inputs,
                executor,
                role,
                current,
                "create",
                "--name",
                name,
                "--driver",
                "docker-container",
                # The flag this resource exists for. A `docker-container` builder
                # has a `localhost` of its own, so without it the push to the
                # registry a run publishes to cannot resolve.
                "--driver-opt",
                "network=host",
                "--use",
            )
            _ = _run(inputs, executor, role, current, "inspect", "--bootstrap", name)
        except BaseException as error:
            best_effort(
                error,
                lambda: remove(inputs),
                what=f"cleanup failed buildx builder {name}",
            )
            raise

    def acquire(inputs: TaskInputs) -> str:
        inspected = _run(
            inputs,
            executor,
            role,
            current,
            "inspect",
            name,
            expected=frozenset({0, 1}),
        )
        if inspected.return_code != 0:
            bootstrap(inputs)
            return name
        return "existing"

    def release(inputs: TaskInputs, state: str) -> None:
        if state != "existing":
            remove(inputs)

    return Resource(
        title=f"Acquire {name} buildx builder",
        acquire=acquire,
        release=release,
        requires=requires,
    )
