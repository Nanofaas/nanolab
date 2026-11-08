"""Build Sonata resources and tasks from plain callables."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome


@dataclass(frozen=True, slots=True)
class _Call[T](Task[T]):
    title: str
    fn: Callable[[TaskInputs], T]
    idempotent: bool = False

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[T]:
        return TaskOutcome(value=self.fn(inputs))


@dataclass(frozen=True, slots=True)
class _ReleaseCall[T](Task[None]):
    title: str
    fn: Callable[[TaskInputs, T], object]

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self.fn(inputs, cast("T", inputs.upstream()))
        return TaskOutcome()


def call[T](
    title: str, fn: Callable[[TaskInputs], T], *, idempotent: bool = False
) -> Task[T]:
    """Wrap ``fn(inputs)`` as a task whose outcome carries its return value."""
    return _Call(title, fn, idempotent)


def release_call[T](title: str, fn: Callable[[TaskInputs, T], object]) -> Task[None]:
    """Wrap ``fn(inputs, acquired)`` as a release task."""
    return _ReleaseCall(title, fn)


def resource_of[T](
    title: str,
    acquire: Callable[[TaskInputs], T],
    release: Callable[[TaskInputs, T], object],
    *,
    requires: tuple[Resource[Any], ...] = (),
    always_release: bool = False,
    revive: Callable[[Any], T] | None = None,
    idempotent: bool = False,
) -> Resource[T]:
    """Build a `Resource` from an acquire and a release callable."""
    return Resource(
        title,
        call(title, acquire, idempotent=idempotent),
        release_call(f"Release {title.removeprefix('Acquire ')}", release),
        requires=requires,
        always_release=always_release,
        revive=revive,
    )


def acquire_now[T](resource: Resource[T], inputs: TaskInputs) -> T:
    """Acquire a resource by hand, outside a workflow."""
    return cast("T", resource.acquire.run(inputs).value)


def release_now[T](resource: Resource[T], inputs: TaskInputs, value: T) -> None:
    """Release a resource by hand, handing the release task ``value``."""
    # ponytail: Sonata has no public "release by hand"; one place reads the
    # private _upstream field
    resource.release.run(dataclasses.replace(inputs, _upstream=value))
