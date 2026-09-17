"""The local docker resources a run owns: the builder, and the registry.

The pinned catalogue's resources do almost this. Each one leaves something
behind, and each is wrapped here for exactly that reason: the builder cannot be
created with the `network=host` publishing to a local registry needs, and the
registry's data outlives the container that held it.
"""

from __future__ import annotations

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.models import TaskResult

from nanolab.tasks.deployment import REGISTRY_CONTAINER_NAME
from nanolab.tasks.soak.local_resources import (
    HELPER_BUILDER,
    helper_builder_resource,
    local_registry_resource,
)


class Executor:
    """Record every buildx argv and answer as the daemon would."""

    def __init__(self, *, present: bool = False, create_fails: bool = False):
        self.commands: list[tuple[str, ...]] = []
        self.present = present
        self.create_fails = create_fails

    def binding_key(self, role: str) -> str:
        return f"test:{role}"

    def run(self, task, *, dry_run: bool = False) -> TaskResult:
        del dry_run
        argv = tuple(task.argv)
        self.commands.append(argv)
        code = 0
        if argv[:3] == ("docker", "buildx", "inspect") and "--bootstrap" not in argv:
            # `inspect` reports a missing builder with a nonzero code, which the
            # resource expects rather than treats as a failure.
            code = 0 if self.present else 1
        elif argv[:3] == ("docker", "buildx", "create") and self.create_fails:
            code = 1
        expected = getattr(task.options, "expected_exit_codes", frozenset({0}))
        return TaskResult(
            task_id="",
            status="passed" if code in expected else "failed",
            return_code=code,
            stdout="",
        )


def acquire(executor: Executor, inputs: TaskInputs, **kwargs) -> str:
    """Acquire the resource the way a workflow would, returning its state."""
    resource = helper_builder_resource(executor=executor, **kwargs)
    return resource.acquire(inputs)


def test_a_missing_builder_is_created_with_host_networking():
    """The flag this resource exists for, and the order it has to happen in.

    A `docker-container` builder runs buildkitd in its own container, so its
    `localhost` is itself and the push to a run's registry cannot resolve
    without host networking.
    """
    executor = Executor(present=False)

    state = acquire(executor, TaskInputs.empty())

    assert state == HELPER_BUILDER
    create = next(argv for argv in executor.commands if argv[2] == "create")
    joined = " ".join(create)
    assert f"--name {HELPER_BUILDER}" in joined
    assert "--driver docker-container" in joined
    assert "--driver-opt network=host" in joined
    assert "--use" in create
    assert executor.commands[0][:4] == ("docker", "buildx", "inspect", HELPER_BUILDER)
    assert "--bootstrap" in executor.commands[-1]


def test_an_existing_builder_is_adopted_and_left_running():
    """An operator's own builder is what the run should use, not replace."""
    executor = Executor(present=True)
    inputs = TaskInputs.empty()
    resource = helper_builder_resource(executor=executor)

    state = resource.acquire(inputs)

    assert state == "existing"
    assert [argv[2] for argv in executor.commands] == ["inspect"]
    resource.release(inputs, state)
    assert not any(argv[2] == "rm" for argv in executor.commands)


def test_a_created_builder_is_removed_on_release():
    executor = Executor(present=False)
    inputs = TaskInputs.empty()
    resource = helper_builder_resource(executor=executor)

    state = resource.acquire(inputs)
    resource.release(inputs, state)

    assert executor.commands[-1] == (
        "docker",
        "buildx",
        "rm",
        "--force",
        HELPER_BUILDER,
    )


def test_a_failed_creation_removes_what_it_started():
    """A half-created builder left behind would be adopted by the next run."""
    executor = Executor(present=False, create_fails=True)

    with pytest.raises(RuntimeError, match=r"buildx create .* failed \(exit 1\)"):
        acquire(executor, TaskInputs.empty())

    assert executor.commands[-1] == (
        "docker",
        "buildx",
        "rm",
        "--force",
        HELPER_BUILDER,
    )


def test_the_name_is_the_one_the_run_builds_with():
    """The plan and the run must not name different builders.

    A run that builds with a builder nothing acquired fails at the first push,
    which is why the plan takes the name from the same options the run does.
    """
    executor = Executor(present=False)

    assert acquire(executor, TaskInputs.empty()) == HELPER_BUILDER
    assert acquire(executor, TaskInputs.empty(), name="custom") == "custom"


class RegistryExecutor:
    """Answer the registry resource's own commands: inspect, run, stop, rm."""

    def __init__(self, *, running: bool = False, present: bool = False):
        self.commands: list[tuple[str, ...]] = []
        self.running = running
        self.present = present

    def binding_key(self, role: str) -> str:
        return f"test:{role}"

    def run(self, task, *, dry_run: bool = False) -> TaskResult:
        del dry_run
        argv = tuple(task.argv)
        self.commands.append(argv)
        if argv[:2] == ("docker", "inspect"):
            # Absent, stopped or running: the pinned acquire reads this and
            # decides whether to run the container, start it, or adopt it.
            code = 0 if self.present else 1
            stdout = f"{str(self.running).lower()}\n" if self.present else ""
        else:
            code, stdout = 0, ""
        expected = getattr(task.options, "expected_exit_codes", frozenset({0}))
        return TaskResult(
            task_id="",
            status="passed" if code in expected else "failed",
            return_code=code,
            stdout=stdout,
        )


def registry(executor: RegistryExecutor, inputs: TaskInputs, **kwargs):
    """Acquire the registry the way a workflow would, returning what it reported."""
    resource = local_registry_resource(executor=executor, ready=lambda: True, **kwargs)
    return resource, resource.acquire(inputs)


def test_the_registry_data_is_removed_with_the_container_it_created():
    """The volume is the leak: a run's pushed layers, left behind per run.

    `docker_registry_resource` releases with `rm --force` and no `-v`, and the
    registry image declares `VOLUME /var/lib/registry`, so docker gives every
    registry container an anonymous volume holding everything the run pushed.
    Removing the container without it leaves that volume dangling -- invisible to
    every later run, because each gets a fresh one, and never reclaimed.
    """
    executor = RegistryExecutor(present=False)
    inputs = TaskInputs.empty()
    resource, state = registry(executor, inputs)

    assert state == "created"
    resource.release(inputs, state)

    assert executor.commands[-1] == (
        "docker",
        "rm",
        "--force",
        "-v",
        REGISTRY_CONTAINER_NAME,
    )


def test_a_registry_this_run_only_started_keeps_its_data():
    """`started` means the data predates the run, so `stop` is the whole job."""
    executor = RegistryExecutor(present=True, running=False)
    inputs = TaskInputs.empty()
    resource, state = registry(executor, inputs)

    assert state == "started"
    resource.release(inputs, state)

    assert executor.commands[-1] == ("docker", "stop", REGISTRY_CONTAINER_NAME)


def test_a_registry_this_run_found_running_is_left_alone():
    executor = RegistryExecutor(present=True, running=True)
    inputs = TaskInputs.empty()
    resource, state = registry(executor, inputs)

    assert state == "existing"
    resource.release(inputs, state)

    assert not any(argv[1] in {"rm", "stop"} for argv in executor.commands)


def test_the_acquire_is_the_pinned_resources_own():
    """Only the release is overridden, so the plan tests' title still names it."""
    executor = RegistryExecutor(present=True, running=True)

    resource, _ = registry(executor, TaskInputs.empty())

    assert resource.title == "Acquire local registry"
