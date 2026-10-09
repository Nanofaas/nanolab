import json
from pathlib import Path

import pytest
from sonata_engine import Resource, TaskInputs
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.recipes.validation import RecipeImageCheckTask
from nanolab.tasks.recipes.workflow import (
    RecipeComponent,
    RecipeDistribution,
    RecipeImage,
)
from nanolab.tasks.resources import ContainerResourceCheckTask
from nanolab.tasks.validation.workflow import (
    ValidateFunction,
    ValidateWorkflowRequest,
    build_validate_workflow,
)

FUNCTION = "word-stats-rust"
IDENTIFIER = "a" * 64
HOST_CONFIG = {
    "CpuShares": 256,
    "NanoCpus": 750_000_000,
    "MemoryReservation": 64 * 1024 * 1024,
    "Memory": 128 * 1024 * 1024,
}
SPEC = {
    "requests": {"cpu": 0.25, "memoryMiB": 64},
    "limits": {"cpu": 0.75, "memoryMiB": 128},
}


class ContainerEngine:
    def __init__(self, name: str) -> None:
        self.seen: list[CommandTaskSpec] = []
        self.ids = [IDENTIFIER]
        self.object = {
            "Id": IDENTIFIER,
            "Name": "/" + name,
            "Image": "sha256:expected",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "io.nanofaas.managed": "true",
                    "io.nanofaas.function": FUNCTION,
                    "io.nanofaas.replica": "1",
                }
            },
            "HostConfig": HOST_CONFIG,
        }

    def binding_key(self, role: str) -> str:
        return role

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        if task.argv[:2] == ("docker", "ps"):
            stdout = "\n".join(self.ids)
        elif task.argv[:2] == ("docker", "inspect"):
            if task.argv[-1] not in (IDENTIFIER, self.object["Name"].lstrip("/")):
                return TaskResult(
                    task_id="",
                    status="failed",
                    return_code=1,
                    stderr="error: no such object",
                )
            if "{{.Image}}" in task.argv:
                stdout = self.object["Image"]
            elif "--format={{json .HostConfig}}" in task.argv:
                stdout = json.dumps(self.object["HostConfig"])
            else:
                stdout = json.dumps(self.object)
        else:
            raise AssertionError(task.argv)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=stdout)


def check(consumer: str, engine: ContainerEngine, tmp_path: Path) -> None:
    if consumer == "resources":
        plan = build_validate_workflow(
            ValidateWorkflowRequest(
                backend="container",
                functions=(
                    ValidateFunction(
                        name=FUNCTION,
                        image="unused",
                        payload="{}",
                        build_argv=("true",),
                        resources=SPEC,
                    ),
                ),
            ),
            RoleBindings({"host": engine}),
        )
        task = next(
            e.task
            for e in plan.compile().tasks
            if isinstance(e.task, ContainerResourceCheckTask)
        )
        task.run(TaskInputs.empty())
    else:
        component = RecipeComponent(
            kind="function",
            name="word-stats",
            sdk="rust",
            mode=None,
            variant=None,
            optimization=None,
            image=RecipeImage(
                reference="unused", id="sha256:expected", status="built", digest=None
            ),
        )
        value = RecipeDistribution(
            report=tmp_path / "report.json",
            recipe_sha256="unused",
            tag="test",
            source=None,
            modules=(),
            components=(component,),
        )
        resource = Resource(
            title="distribution",
            acquire=lambda _inputs: value,
            release=lambda _inputs, _value: None,
        )
        RecipeImageCheckTask(
            resource,
            executor=engine,
            run_dir=tmp_path,
            project=DockerComposeProject(
                name="test",
                file=tmp_path / "compose.yaml",
                ready_url="http://localhost/ready",
            ),
            cwd=tmp_path,
            function=(FUNCTION, "word-stats", "rust"),
        ).run(TaskInputs._for_resources({resource: value}, {resource}))


@pytest.mark.parametrize("consumer", ["recipe", "resources"])
@pytest.mark.parametrize(
    "name",
    ["nanofaas-word-stats-rust-r1", "nanofaas-word-stats-rust-66660a04b3f1a342-r1"],
)
def test_inspections_find_legacy_and_hashed_names_by_labels(
    consumer: str, name: str, tmp_path: Path
) -> None:
    engine = ContainerEngine(name)
    check(consumer, engine, tmp_path)
    query = next(e.argv for e in engine.seen if e.argv[:2] == ("docker", "ps"))
    for label in (
        "io.nanofaas.managed=true",
        f"io.nanofaas.function={FUNCTION}",
        "io.nanofaas.replica=1",
    ):
        assert f"label={label}" in query
    assert "--no-trunc" in query
    inspections = [e.argv for e in engine.seen if e.argv[:2] == ("docker", "inspect")]
    assert inspections and all(e[-1] == IDENTIFIER for e in inspections)


@pytest.mark.parametrize("consumer", ["recipe", "resources"])
@pytest.mark.parametrize("ids", [[], [IDENTIFIER, "b" * 64]])
def test_inspections_reject_missing_or_ambiguous_replica(
    consumer: str, ids: list[str], tmp_path: Path
) -> None:
    engine = ContainerEngine("nanofaas-word-stats-rust-r1")
    engine.ids = ids
    with pytest.raises(RuntimeError, match="exactly one"):
        check(consumer, engine, tmp_path)
    assert all(e.argv[:2] != ("docker", "inspect") for e in engine.seen)


@pytest.mark.parametrize("consumer", ["recipe", "resources"])
@pytest.mark.parametrize(
    "field", ["function", "replica", "managed", "running", "identity"]
)
def test_inspections_revalidate_selected_container_identity(
    consumer: str, field: str, tmp_path: Path
) -> None:
    engine = ContainerEngine("nanofaas-word-stats-rust-r1")
    if field == "running":
        engine.object["State"]["Running"] = False
    elif field == "identity":
        engine.object["Id"] = "b" * 64
    else:
        engine.object["Config"]["Labels"][f"io.nanofaas.{field}"] = "wrong"
    with pytest.raises(RuntimeError, match="identity"):
        check(consumer, engine, tmp_path)


@pytest.mark.parametrize("consumer", ["recipe", "resources"])
@pytest.mark.parametrize("identifier", ["abc123", "container-name"])
def test_inspections_reject_a_non_full_container_id(
    consumer: str, identifier: str, tmp_path: Path
) -> None:
    engine = ContainerEngine("unused")
    engine.ids = [identifier]
    with pytest.raises(RuntimeError, match="identity"):
        check(consumer, engine, tmp_path)
    assert all(e.argv[:2] != ("docker", "inspect") for e in engine.seen)


def test_resources_select_the_requested_replica_without_a_resource_spec() -> None:
    engine = ContainerEngine("any-name")
    engine.object["Config"]["Labels"]["io.nanofaas.replica"] = "2"
    task = ContainerResourceCheckTask(
        function=FUNCTION, replica=2, resources=None, executor=engine, role="host"
    )
    task.run(TaskInputs.empty())
    assert "label=io.nanofaas.replica=2" in engine.seen[0].argv
    engine.object["State"]["Running"] = False
    with pytest.raises(RuntimeError, match="identity"):
        task.run(TaskInputs.empty())


@pytest.mark.parametrize(
    "field", ["function", "replica", "resources", "role", "cwd", "binding"]
)
def test_resource_check_resume_identity_covers_selection_and_expectations(
    field: str, tmp_path: Path
) -> None:
    from sonata_engine import Workflow

    def fingerprint(**overrides: object) -> str:
        arguments = {
            "function": FUNCTION,
            "replica": 1,
            "resources": SPEC,
            "executor": ContainerEngine("unused"),
            "role": "host",
            "cwd": tmp_path,
        }
        arguments.update(overrides)
        workflow = Workflow(workflow_id="inspection")
        workflow.add(ContainerResourceCheckTask(**arguments))
        return workflow.compile().fingerprint

    class OtherBinding(ContainerEngine):
        def binding_key(self, role: str) -> str:
            return f"another:{role}"

    changed = {
        "function": {"function": "another-function"},
        "replica": {"replica": 2},
        "resources": {
            "resources": {
                "requests": SPEC["requests"],
                "limits": {**SPEC["limits"], "cpu": 1.0},
            }
        },
        "role": {"role": "stack"},
        "cwd": {"cwd": tmp_path / "another"},
        "binding": {"executor": OtherBinding("unused")},
    }
    assert fingerprint() == fingerprint()
    assert fingerprint() != fingerprint(**changed[field])
