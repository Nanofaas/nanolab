from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import pytest
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome, Workflow
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from nanolab.tasks.compose import DockerComposeProject, isolated_compose_resource


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)
    fail_readiness: bool = False

    def binding_key(self, role: str) -> str:
        return f"test:{role}"

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        failed = self.fail_readiness and task.argv[0] == "curl"
        return TaskResult(
            task_id=task.task_id,
            status="failed" if failed else "passed",
            return_code=7 if failed else 0,
            stderr="not ready" if failed else "",
        )


@pytest.mark.parametrize("fail_readiness", [False, True])
def test_isolated_compose_keeps_consumer_policy_value_and_dependency_lifecycle(
    tmp_path: Path, fail_readiness: bool
) -> None:
    executor = RecordingExecutor(fail_readiness=fail_readiness)
    project = DockerComposeProject(
        "experiment",
        Path("compose.yaml"),
        "http://ready",
        build=False,
        role="stack",
        env={"IMAGE": "application:trial"},
    )
    events: list[str] = []
    dependency = Resource(
        title="Prepare",
        acquire=lambda _inputs: events.append("prepare"),
        release=lambda _inputs, _value: events.append("release"),
    )
    resource = isolated_compose_resource(
        project,
        executor=executor,
        cwd=tmp_path,
        requires=(dependency,),
    )

    class UseProject(Task[None]):
        title = "Use project"

        @override
        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            assert inputs.resource(resource) is project
            assert inputs.resource(resource).role == "stack"
            events.append("use")
            return TaskOutcome(value=None)

    workflow = Workflow("experiment")
    workflow.add(UseProject(), requires=(resource,))
    if fail_readiness:
        with pytest.raises(RuntimeError, match="not ready"):
            workflow.run()
        assert events == ["prepare", "release"]
    else:
        workflow.run()
        assert events == ["prepare", "use", "release"]
    assert [
        "curl" if task.argv[0] == "curl" else task.argv[6] for task in executor.seen
    ] == ["down", "up", "curl", "down"]
    assert executor.seen[0].argv == executor.seen[-1].argv
    assert executor.seen[0].argv[-2:] == ("--volumes", "--remove-orphans")
    assert "--build" not in executor.seen[1].argv
    assert all(
        task.role == "stack"
        and task.options.cwd == tmp_path
        and task.options.env == project.env
        for task in executor.seen
    )
