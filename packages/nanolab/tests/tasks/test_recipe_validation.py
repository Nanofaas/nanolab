from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from sonata_engine import Resource, TaskInputs, Workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.platform import Backend, PlatformFunction, add_platform
from nanolab.tasks.recipe import (
    RecipeBinding,
    RecipeComponent,
    RecipeDistribution,
    RecipeImage,
)
from nanolab.tasks.recipe_validation import (
    RecipeFunctionRegisterTask,
    RecipeImageCheckTask,
    RecipeMetadataCheckTask,
    recipe_compose_resource,
)
from nanolab.tasks.validate import (
    ValidateFunction,
    ValidateWorkflowRequest,
    build_validate_workflow,
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.specs: list[CommandTaskSpec] = []

    def binding_key(self, role: str) -> str:
        return role

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.specs.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout="ok")


@pytest.mark.parametrize("backend", ["container", "containerd", "k8s"])
@pytest.mark.parametrize("attach_later", [False, True])
def test_recipe_owns_platform_builds(
    tmp_path: Path, backend: Backend, attach_later: bool
) -> None:
    def unexpected_acquire(_inputs: TaskInputs) -> RecipeDistribution:
        raise AssertionError("Compilation must not publish a recipe")

    published = Resource(
        title="Published recipe",
        acquire=unexpected_acquire,
        release=lambda _inputs, _value: None,
    )
    binding = RecipeBinding(
        distribution=published,
        functions={"word-stats-java": ("word-stats", "java")},
        run_dir=tmp_path / "run",
    )
    function = PlatformFunction(
        name="word-stats-java",
        image="unused:tag",
        payload="{}",
        build_argv=("./gradlew", "bootJar"),
        image_build_argv=("docker", "build", "."),
    )
    request = ValidateWorkflowRequest(
        backend=backend,
        functions=(function,),
        push_function_images=True,
        recipe=None if attach_later else binding,
    )
    if attach_later:
        request = replace(request, recipe=binding)
    executor = RecordingExecutor()
    workflow = Workflow(workflow_id="recipe-build-ownership")
    platform = add_platform(workflow, request, executor=executor)
    workflow.add(
        CommandTask(title="Use platform", argv=("true",), executor=executor),
        requires=platform.functions,
    )

    titles = [entry.task.title for entry in workflow.compile().tasks]
    assert not any(title.startswith(("Build", "Push")) for title in titles)
    assert titles.index("Published recipe") < titles.index("Acquire word-stats-java")
    assert "Acquire word-stats-java" in titles
    assert not (request.build_images or request.build_control_plane)
    assert not request.push_function_images
    assert executor.specs == []
    assert not binding.run_dir.exists()


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


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("not-json", "Invalid build metadata response"),
        (
            json.dumps(
                {
                    "modules": ["build-metadata", "container-deployment-provider"],
                    "build": {
                        "type": "jvm",
                        "variant": "wrong-variant",
                        "optimization": "c2",
                    },
                }
            ),
            "differs from recipe",
        ),
    ],
)
def test_failed_metadata_check_retains_raw_response(
    tmp_path: Path, body: str, message: str
) -> None:
    value = distribution(tmp_path)
    resource = Resource(
        title="distribution",
        acquire=lambda _inputs: value,
        release=lambda _inputs, _value: None,
    )
    inputs = TaskInputs._for_resources({resource: value}, {resource})

    class MetadataExecutor(RecordingExecutor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            self.specs.append(task)
            return TaskResult(task_id="", status="passed", return_code=0, stdout=body)

    with pytest.raises(RuntimeError, match=message):
        RecipeMetadataCheckTask(
            resource,
            executor=MetadataExecutor(),
            run_dir=tmp_path,
            endpoint="http://127.0.0.1:8080",
        ).run(inputs)

    assert (tmp_path / "build-metadata.json").read_text() == body


def test_failed_image_check_retains_inspected_image_id(tmp_path: Path) -> None:
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
            output = "container-1" if "ps" in task.argv else "sha256:unexpected"
            return TaskResult(task_id="", status="passed", return_code=0, stdout=output)

    project = DockerComposeProject(
        name="nanofaas-validate",
        file=Path("deploy/compose/compose.yaml"),
        ready_url="http://localhost:8081/ready",
    )
    with pytest.raises(RuntimeError, match="sha256:unexpected"):
        RecipeImageCheckTask(
            resource,
            executor=ImageExecutor(),
            run_dir=tmp_path,
            project=project,
            cwd=tmp_path,
        ).run(inputs)

    assert json.loads(
        (tmp_path / "image-control-plane-control-plane.json").read_text()
    ) == {
        "container": "container-1",
        "imageId": "sha256:unexpected",
        "reference": "registry/control-plane:run-1",
    }


def test_missing_compose_container_retains_observed_response(tmp_path: Path) -> None:
    value = distribution(tmp_path)
    resource = Resource(
        title="distribution",
        acquire=lambda _inputs: value,
        release=lambda _inputs, _value: None,
    )
    inputs = TaskInputs._for_resources({resource: value}, {resource})

    class EmptyComposeExecutor(RecordingExecutor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            self.specs.append(task)
            return TaskResult(task_id="", status="passed", return_code=0, stdout="")

    project = DockerComposeProject(
        name="nanofaas-validate",
        file=Path("deploy/compose/compose.yaml"),
        ready_url="http://localhost:8081/ready",
    )
    with pytest.raises(RuntimeError, match="container was not found"):
        RecipeImageCheckTask(
            resource,
            executor=EmptyComposeExecutor(),
            run_dir=tmp_path,
            project=project,
            cwd=tmp_path,
        ).run(inputs)

    assert json.loads((tmp_path / "compose-ps-control-plane.json").read_text()) == {
        "stdout": ""
    }


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("compose-ready", "failed"),
        ("metadata", "metadata differs from recipe"),
        ("control-image", "sha256:unexpected"),
        ("invoke", "invocation did not report success"),
        ("function-image", "sha256:unexpected"),
    ],
)
def test_recipe_failure_releases_acquired_resources(
    tmp_path: Path, failure: str, message: str
) -> None:
    events: list[str] = []
    value = distribution(tmp_path)
    registry = Resource(
        title="Local registry",
        acquire=lambda _inputs: events.append("registry-up"),
        release=lambda _inputs, _value: events.append("registry-down"),
    )
    published = Resource(
        title="Published recipe",
        acquire=lambda _inputs: value,
        release=lambda _inputs, _value: None,
        requires=(registry,),
    )
    project = DockerComposeProject(
        name="nanofaas-validate",
        file=Path("deploy/compose/compose.yaml"),
        ready_url="http://localhost:8081/ready",
    )

    class FailureExecutor(RecordingExecutor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            self.specs.append(task)
            command = " ".join(task.argv)
            events.append(command)
            if failure == "compose-ready" and project.ready_url in command:
                return TaskResult(task_id="", status="failed", return_code=1)
            if "/modules/build-metadata" in command:
                variant = "wrong" if failure == "metadata" else "recipe-v2-jvm"
                output = json.dumps(
                    {
                        "modules": ["build-metadata", "container-deployment-provider"],
                        "build": {
                            "type": "jvm",
                            "variant": variant,
                            "optimization": "c2",
                        },
                    }
                )
            elif " ps -q control-plane" in command:
                output = "container-1"
            elif "docker inspect --format {{.Image}}" in command:
                is_function = "nanofaas-word-stats-java-r1" in command
                output = "sha256:fn" if is_function else "sha256:cp"
                if failure == ("function-image" if is_function else "control-image"):
                    output = "sha256:unexpected"
            elif ":invoke" in command:
                status = "error" if failure == "invoke" else "success"
                output = json.dumps({"status": status, "output": "ok"})
            else:
                output = "ok"
            return TaskResult(task_id="", status="passed", return_code=0, stdout=output)

    executor = FailureExecutor()
    compose = recipe_compose_resource(
        project,
        distribution=published,
        executor=executor,
        cwd=tmp_path,
        requires=(registry,),
    )
    binding = RecipeBinding(
        distribution=published,
        functions={"word-stats-java": ("word-stats", "java")},
        project=project,
        run_dir=tmp_path,
    )
    function = ValidateFunction(
        name="word-stats-java",
        image="unused:tag",
        payload='{"text":"a b"}',
        build_argv=("true",),
    )
    workflow = build_validate_workflow(
        ValidateWorkflowRequest(
            backend="container",
            functions=(function,),
            build_images=False,
            recipe=binding,
        ),
        RoleBindings({"host": executor}),
        cwd=tmp_path,
        requires=(registry, published, compose),
    )

    with pytest.raises(RuntimeError, match=message):
        workflow.run()

    assert events[0] == "registry-up"
    assert events[-1] == "registry-down"
    down = [
        i
        for i, event in enumerate(events)
        if "docker compose" in event and " down " in event
    ]
    assert len(down) == 2  # pre-run clear, then compensation or release
    assert down[-1] < len(events) - 1
    deletes = [
        i for i, event in enumerate(events) if "curl" in event and "-X DELETE" in event
    ]
    if failure in {"invoke", "function-image"}:
        assert len(deletes) == 1
        assert deletes[0] < down[-1]
    else:
        assert not deletes


@pytest.mark.parametrize("catalog_sdk", ["exec", "bash"])
def test_bash_recipe_registers_published_image_for_exec_catalog(
    tmp_path: Path, catalog_sdk: str
) -> None:
    from dataclasses import replace

    from nanolab.tasks.recipe import require_validation_distribution

    original = distribution(tmp_path)
    function_component = replace(
        original.components[1],
        sdk="bash",
        mode="container",
        image=replace(
            original.components[1].image,
            reference="127.0.0.1:5000/nanofaas/bash-word-stats:run-1",
        ),
    )
    control_plane = replace(
        original.components[0],
        image=replace(
            original.components[0].image,
            reference="127.0.0.1:5000/nanofaas/control-plane:run-1",
        ),
    )
    value = replace(original, components=(control_plane, function_component))
    require_validation_distribution(value, functions=(("word-stats", catalog_sdk),))
    resource = Resource(
        title="Bash distribution",
        acquire=lambda _inputs: value,
        release=lambda _inputs, _value: None,
    )
    executor = RecordingExecutor()
    function = PlatformFunction(
        name="word-stats-exec",
        image="wrong-default:tag",
        payload="{}",
        build_argv=("true",),
    )
    RecipeFunctionRegisterTask(
        function,
        recipe_name="word-stats",
        sdk=catalog_sdk,
        distribution=resource,
        endpoint="http://127.0.0.1:8080",
        executor=executor,
        role="host",
    ).run(TaskInputs._for_resources({resource: value}, {resource}))
    registration = executor.specs[-1]
    payload = json.loads(registration.argv[registration.argv.index("--data") + 1])
    assert payload["image"] == "127.0.0.1:5000/nanofaas/bash-word-stats:run-1"
    assert payload["name"] == "word-stats-exec"
    with pytest.raises(ValueError, match="selected functions"):
        require_validation_distribution(value, functions=(("word-stats", "python"),))


def test_recipe_service_selection_and_registration_keep_component_kind(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from nanolab.tasks.recipe import require_validation_distribution

    original = distribution(tmp_path)
    components = tuple(
        replace(
            c,
            image=replace(c.image, reference=f"127.0.0.1:5000/nanofaas/{c.name}:run-1"),
        )
        for c in original.components
    )
    service = replace(
        components[1],
        kind="service",
        image=replace(
            components[1].image, reference="127.0.0.1:5000/nanofaas/service:run-1"
        ),
    )
    value = replace(original, components=(*components, service))
    require_validation_distribution(
        value, functions=(("word-stats", "java"),), services=(("word-stats", "java"),)
    )
    with pytest.raises(ValueError, match="selected services"):
        require_validation_distribution(value, functions=(("word-stats", "java"),))
    with pytest.raises(ValueError, match="selected services"):
        require_validation_distribution(
            value, functions=(("word-stats", "java"),), services=(("other", "java"),)
        )
    resource = Resource(
        title="distribution",
        acquire=lambda _inputs: value,
        release=lambda _inputs, _value: None,
    )
    executor = RecordingExecutor()
    RecipeFunctionRegisterTask(
        PlatformFunction(
            name="service-echo", image="wrong", payload="{}", build_argv=("true",)
        ),
        recipe_name="word-stats",
        sdk="java",
        kind="service",
        distribution=resource,
        endpoint="http://localhost:8080",
        executor=executor,
        role="host",
    ).run(TaskInputs._for_resources({resource: value}, {resource}))
    registration = executor.specs[-1]
    payload = json.loads(registration.argv[registration.argv.index("--data") + 1])
    assert payload["image"] == "127.0.0.1:5000/nanofaas/service:run-1"
    assert payload["executionMode"] == "DEPLOYMENT"

    class ImageExecutor(RecordingExecutor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            return TaskResult(
                task_id="", status="passed", return_code=0, stdout="sha256:service"
            )

    service_value = replace(
        value,
        components=(
            *components,
            replace(service, image=replace(service.image, id="sha256:service")),
        ),
    )
    project = DockerComposeProject(
        name="test", file=Path("compose.yaml"), ready_url="http://localhost/ready"
    )
    check = RecipeImageCheckTask(
        resource,
        executor=ImageExecutor(),
        run_dir=tmp_path,
        project=project,
        cwd=tmp_path,
        function=("service-echo", "word-stats", "java"),
        kind="service",
    )
    check.run(TaskInputs._for_resources({resource: service_value}, {resource}))
    evidence = json.loads((tmp_path / "image-service-word-stats.json").read_text())
    assert evidence["container"] == "nanofaas-service-echo-r1"
    assert evidence["reference"] == "127.0.0.1:5000/nanofaas/service:run-1"
    with pytest.raises(RuntimeError, match="Running image"):
        check.run(TaskInputs._for_resources({resource: value}, {resource}))
