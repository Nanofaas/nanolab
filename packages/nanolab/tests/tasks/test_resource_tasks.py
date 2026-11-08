from __future__ import annotations

from sonata_engine import TaskInputs, Workflow

from nanolab.tasks.resource_tasks import (
    acquire_now,
    call,
    release_call,
    release_now,
    resource_of,
)


def test_resource_of_runs_in_a_workflow() -> None:
    events: list[object] = []

    def acquire(_: TaskInputs) -> str:
        events.append("acquire")
        return "vm-1"

    def release(_: TaskInputs, value: str) -> None:
        events.append(("release", value))

    vm = resource_of("Acquire vm", acquire, release)
    workflow = Workflow(workflow_id="t")
    workflow.add(
        call("Use vm", lambda inputs: events.append(("use", inputs.resource(vm)))),
        requires=(vm,),
    )
    workflow.run()

    assert events == ["acquire", ("use", "vm-1"), ("release", "vm-1")]


def test_resource_of_keeps_options() -> None:
    base = resource_of("Acquire base", lambda _: 1, lambda _i, _v: None)

    def revive(raw: object) -> int:
        return int(str(raw))

    res = resource_of(
        "Acquire x",
        lambda _: 2,
        lambda _i, _v: None,
        requires=(base,),
        always_release=True,
        revive=revive,
        idempotent=True,
    )

    assert res.requires == (base,)
    assert res.always_release is True
    assert res.revive is revive
    assert res.acquire.idempotent is True
    assert base.acquire.idempotent is False


def test_release_title_follows_the_engine() -> None:
    res = resource_of("Acquire vm", lambda _: None, lambda _i, _v: None)

    assert res.release_title == "Release vm"
    assert res.release.title == "Release vm"
    assert res.acquire.title == "Acquire vm"


def test_acquire_now_and_release_now() -> None:
    released: list[str] = []
    res = resource_of("Acquire vm", lambda _: "vm-2", lambda _i, v: released.append(v))
    inputs = TaskInputs.empty()

    value = acquire_now(res, inputs)
    release_now(res, inputs, value)

    assert value == "vm-2"
    assert released == ["vm-2"]


def test_release_now_passes_none() -> None:
    seen: list[object] = []
    res = resource_of("Acquire t", lambda _: None, lambda _i, v: seen.append(v))

    release_now(res, TaskInputs.empty(), None)

    assert seen == [None]


def test_call_and_release_call_titles() -> None:
    assert call("Do it", lambda _: 3).title == "Do it"
    assert call("Do it", lambda _: 3, idempotent=True).idempotent is True
    assert call("Do it", lambda _: 3).run(TaskInputs.empty()).value == 3
    assert release_call("Release it", lambda _i, _v: None).title == "Release it"
