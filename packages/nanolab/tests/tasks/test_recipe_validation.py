from __future__ import annotations

import json
from pathlib import Path

from sonata_engine import TaskInputs
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.platform import PlatformFunction
from nanolab.tasks.recipe import RecipeComponent, RecipeDistribution, RecipeImage
from nanolab.tasks.recipe_validation import (
    RecipeFunctionRegisterTask,
    recipe_compose_resource,
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.specs: list[CommandTaskSpec] = []

    def binding_key(self, role: str) -> str:
        return role

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.specs.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout="ok")


def distribution(tmp_path: Path) -> RecipeDistribution:
    return RecipeDistribution(
        tmp_path / "distribution.json",
        "abc",
        "run-1",
        None,
        ("build-metadata", "container-deployment-provider"),
        (
            RecipeComponent(
                "control-plane",
                "control-plane",
                "java",
                "jvm",
                RecipeImage(
                    "registry/control-plane:run-1",
                    "sha256:cp",
                    "published",
                    "sha256:c1",
                ),
                "recipe-v2-jvm",
                "c2",
            ),
            RecipeComponent(
                "function",
                "word-stats",
                "java",
                "jvm",
                RecipeImage(
                    "registry/word-stats:run-1", "sha256:fn", "published", "sha256:f1"
                ),
                None,
                None,
            ),
        ),
    )


def test_recipe_registers_report_image(tmp_path: Path) -> None:
    from sonata_engine import Resource

    value = distribution(tmp_path)
    resource = Resource(
        title="distribution",
        acquire=lambda _inputs: value,
        release=lambda _inputs, _value: None,
    )
    inputs = TaskInputs._for_resources({resource: value}, {resource})
    executor = RecordingExecutor()
    function = PlatformFunction(
        name="word-stats-java",
        image="wrong-default:tag",
        payload="{}",
        build_argv=("true",),
    )

    RecipeFunctionRegisterTask(
        function,
        recipe_name="word-stats",
        sdk="java",
        distribution=resource,
        endpoint="http://127.0.0.1:8080",
        executor=executor,
        role="host",
    ).run(inputs)

    register = next(
        spec for spec in executor.specs if "/v1/functions" in " ".join(spec.argv)
    )
    payload = json.loads(register.argv[register.argv.index("--data") + 1])
    assert payload["image"] == "registry/word-stats:run-1"
    assert payload["name"] == "word-stats-java"


def test_recipe_compose_uses_report_image_without_build(tmp_path: Path) -> None:
    from sonata_engine import Resource

    value = distribution(tmp_path)
    resource = Resource(
        title="distribution",
        acquire=lambda _inputs: value,
        release=lambda _inputs, _value: None,
    )
    inputs = TaskInputs._for_resources({resource: value}, {resource})
    executor = RecordingExecutor()
    project = DockerComposeProject(
        name="nanofaas-validate",
        file=Path("deploy/compose/compose.yaml"),
        ready_url="http://localhost:8081/ready",
    )

    compose = recipe_compose_resource(
        project, distribution=resource, executor=executor, cwd=tmp_path
    )
    compose.acquire(inputs)

    up = next(spec for spec in executor.specs if "up" in spec.argv)
    assert "--no-build" in up.argv
    assert "--build" not in up.argv
    assert (
        up.options.env["NANOFAAS_CONTROL_PLANE_IMAGE"] == "registry/control-plane:run-1"
    )


def test_validation_rejects_unselected_recipe_component(tmp_path: Path) -> None:
    from dataclasses import replace

    from nanolab.tasks.recipe import require_validation_distribution

    value = distribution(tmp_path)
    extra = RecipeComponent(
        "function",
        "unexpected",
        "java",
        "jvm",
        RecipeImage(
            "registry/unexpected:run-1", "sha256:extra", "published", "sha256:ex"
        ),
        None,
        None,
    )
    with __import__("pytest").raises(ValueError, match="selected functions"):
        require_validation_distribution(
            replace(value, components=(*value.components, extra)),
            functions=(("word-stats", "java"),),
        )


def test_control_plane_image_check_uses_compose_working_directory(
    tmp_path: Path,
) -> None:
    from sonata_engine import Resource

    from nanolab.tasks.recipe_validation import RecipeImageCheckTask

    value = distribution(tmp_path)
    resource = Resource(
        title="distribution",
        acquire=lambda _inputs: value,
        release=lambda _inputs, _value: None,
    )
    inputs = TaskInputs._for_resources({resource: value}, {resource})

    class ImageExecutor(RecordingExecutor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            self.specs.append(task)
            output = "container-1" if "ps" in task.argv else "sha256:cp"
            return TaskResult(task_id="", status="passed", return_code=0, stdout=output)

    executor = ImageExecutor()
    project = DockerComposeProject(
        name="nanofaas-validate",
        file=Path("deploy/compose/compose.yaml"),
        ready_url="http://localhost:8081/ready",
    )
    RecipeImageCheckTask(
        resource, executor=executor, run_dir=tmp_path, project=project, cwd=tmp_path
    ).run(inputs)
    compose_ps = next(spec for spec in executor.specs if "ps" in spec.argv)
    assert compose_ps.options.cwd == tmp_path
