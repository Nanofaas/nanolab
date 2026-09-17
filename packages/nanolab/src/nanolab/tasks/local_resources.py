"""The local docker resources a soak or heap-analysis run owns, for its duration.

Both are needed before anything is built: preparation pushes application images
and the helper build pushes the helper into the registry, building the helper
through the builder. Each is acquired before the measuring task and released
after it, and each removes only what it created or started, so an operator's own
registry or builder is adopted and left alone.

Both wrap a pinned resource from `sonata_tasks`, and in both cases the wrapper
exists because that resource leaves something behind:

- The builder needs `network=host` on its own container to publish to a local
  registry, which is a `--driver-opt` given at creation, and
  `buildx_builder_resource` passes no driver options. A `buildkitd_config` does
  not substitute -- measured: `[worker.oci] networkMode = "host"` leaves the
  builder container on the bridge network, because that key governs the network
  of build steps rather than the daemon that pushes. So the create argv lives
  here, and buildx's own "inspect, then create or adopt" semantics are kept.
- The registry is started with no volume mount, so the registry image's
  `VOLUME /var/lib/registry` makes docker create an anonymous volume holding
  every layer the run pushes -- 1.3 GB in a measured run.
  `docker_registry_resource` releases by removing the container without `-v`,
  which leaves that volume behind: one per run, invisible to every later run
  because each gets a fresh anonymous volume, and never reclaimed. The acquire is
  that resource's, unchanged; only the release differs, and only where it left
  something behind.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import best_effort
from sonata_tasks.execution.models import CommandOptions, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor

from nanolab.tasks.deployment import REGISTRY_CONTAINER_NAME

# The builder a run looks for when nothing overrides it, shared with the runtimes
# that build through it: the plan and the run must never name different builders,
# or the run builds with one nothing acquired.
HELPER_BUILDER = "nanolab-heap-analysis"


def _docker(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: str,
    options: CommandOptions,
    *args: str,
    expected: frozenset[int] = frozenset({0}),
) -> TaskResult:
    """Run one docker command and return what it reported."""
    argv = ("docker", *args)
    outcome = CommandTask(
        title=" ".join(argv),
        argv=argv,
        executor=executor,
        role=role,
        options=replace(options, expected_exit_codes=expected),
    ).run(inputs)
    if outcome.value is None:
        raise RuntimeError("docker returned no command result")
    return outcome.value


def _buildx(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: str,
    options: CommandOptions,
    *args: str,
    expected: frozenset[int] = frozenset({0}),
) -> TaskResult:
    """Run one docker buildx command and return what it reported."""
    return _docker(inputs, executor, role, options, "buildx", *args, expected=expected)


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
        _ = _buildx(inputs, executor, role, current, "rm", "--force", name)

    def bootstrap(inputs: TaskInputs) -> None:
        try:
            _ = _buildx(
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
            _ = _buildx(inputs, executor, role, current, "inspect", "--bootstrap", name)
        except BaseException as error:
            best_effort(
                error,
                lambda: remove(inputs),
                what=f"cleanup failed buildx builder {name}",
            )
            raise

    def acquire(inputs: TaskInputs) -> str:
        inspected = _buildx(
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


def local_registry_resource(
    *,
    executor: CommandTaskExecutor,
    container: str = REGISTRY_CONTAINER_NAME,
    role: str = "host",
    options: CommandOptions | None = None,
    ready: Callable[[], bool] | None = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[str]:
    """Acquire the local registry, removing its data along with the container.

    Acquiring is the pinned resource's, unchanged: it adopts a registry that is
    already running, starts a stopped one, or runs a missing one, and waits for
    it to answer. Only the release differs -- `docker rm --force -v`, so the
    anonymous volume the registry image creates for `/var/lib/registry` goes with
    the container instead of accumulating a run's worth of layers per run.
    """
    from sonata_tasks.registry import docker_registry_resource

    pinned = docker_registry_resource(
        executor=executor, container=container, role=role, options=options, ready=ready
    )
    current = options or CommandOptions()

    def release(inputs: TaskInputs, state: str) -> None:
        if state == "created":
            # `-v` removes the anonymous volumes attached to this container, and
            # only those: a registry this run merely started keeps its data.
            _ = _docker(
                inputs,
                executor,
                role,
                current,
                "rm",
                "--force",
                "-v",
                container,
                expected=frozenset({0, 1}),
            )
        elif state == "started":
            _ = _docker(
                inputs,
                executor,
                role,
                current,
                "stop",
                container,
                expected=frozenset({0, 1}),
            )

    return Resource(
        title=pinned.title,
        acquire=pinned.acquire,
        release=release,
        requires=requires,
    )
