"""NanoFaaS recipe build commands and their distribution contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, override

import yaml
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor

from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.deployment import LOCAL_REGISTRY
from nanolab.workspace.recipe import RecipeRun, prepare_recipe_run


@dataclass(frozen=True, slots=True)
class RecipeImage:
    """One locally built image and its optional published digest."""

    reference: str
    id: str
    status: str
    digest: str | None


@dataclass(frozen=True, slots=True)
class RecipeComponent:
    """One component selected by a recipe."""

    kind: str
    name: str
    sdk: str
    mode: str | None
    image: RecipeImage
    variant: str | None
    optimization: str | None
    native: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RecipeDistribution:
    """Validated single-platform distribution written by NanoFaaS."""

    report: Path
    recipe_sha256: str
    tag: str
    source: dict[str, object] | None
    modules: tuple[str, ...]
    components: tuple[RecipeComponent, ...]

    def control_plane(self) -> RecipeComponent:
        """Return the one control plane."""
        return self._component("control-plane", "control-plane", "java")

    def function(self, name: str, sdk: str) -> RecipeComponent:
        """Find a function, accepting catalog exec as the recipe Bash SDK."""
        return self._component("function", name, "bash" if sdk == "exec" else sdk)

    def _component(self, kind: str, name: str, sdk: str) -> RecipeComponent:
        matches = [
            c for c in self.components if (c.kind, c.name, c.sdk) == (kind, name, sdk)
        ]
        if len(matches) != 1:
            raise ValueError(f"Recipe distribution expected one {kind} {name}/{sdk}")
        return matches[0]


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Recipe distribution {label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Recipe distribution {label} must be a nonempty string")
    return value


def _verify_recipe_identity(
    recipe: Path, tag: str, modules: list[str], components: list[RecipeComponent]
) -> None:
    """Compare the report's build selection to the actual profile inputs."""
    profile = _object(yaml.safe_load(recipe.read_text(encoding="utf-8")), "profile")
    control = _object(profile.get("controlPlane"), "profile controlPlane")
    selected_modules = control.get("modules", [])
    if sorted(modules) != sorted(selected_modules) or len(modules) != len(set(modules)):
        raise ValueError("Recipe distribution modules differ from recipe")
    registry = profile.get("registry")
    if registry is None:
        prefix = f"nanofaas/{_string(profile.get('name'), 'profile name')}"
        image_tag = "local"
    else:
        prefix = _string(
            _object(registry, "profile registry").get("repository"), "repository"
        )
        image_tag = tag

    expected: dict[tuple[str, str, str], tuple[str | None, str]] = {}

    def add(kind: str, item: dict[str, Any], name: str, sdk: str) -> None:
        build = item.get("build")
        mode = (
            _object(build, f"{name} build").get("mode")
            if build is not None
            else "container"
        )
        container = _object(item.get("container"), f"{name} container")
        image_name = _string(container.get("image"), f"{name} image")
        expected[(kind, name, sdk)] = (mode, f"{prefix}/{image_name}:{image_tag}")

    add("control-plane", control, "control-plane", "java")
    for kind, field in (("function", "functions"), ("service", "services")):
        for item in profile.get(field, []):
            entry = _object(item, f"profile {field} entry")
            add(
                kind,
                entry,
                _string(entry.get("name"), "name"),
                _string(entry.get("sdk"), "sdk"),
            )
    if set(expected) != {(c.kind, c.name, c.sdk) for c in components}:
        raise ValueError("Recipe distribution components differ from recipe")
    for component in components:
        mode, reference = expected[(component.kind, component.name, component.sdk)]
        if component.mode != mode or component.image.reference != reference:
            raise ValueError(
                f"Recipe distribution {component.name} differs from recipe"
            )

    build = _object(control.get("build"), "profile controlPlane build")
    variant = build.get("variant")
    native = _object(build.get("native", {}), "profile native build")
    if variant is not None or "optimization" in native:
        if build.get("mode") == "native":
            optimization = str(native.get("optimization", "3"))
        else:
            jvm = _object(control.get("jvm", {}), "profile controlPlane jvm")
            args = jvm.get("args", [])
            optimization = "c1" if "-XX:TieredStopAtLevel=1" in args else "c2"
    else:
        optimization = None
    control_component = next(c for c in components if c.kind == "control-plane")
    if (
        control_component.variant != variant
        or control_component.optimization != optimization
    ):
        raise ValueError(
            "Recipe distribution control-plane metadata differs from recipe"
        )


def read_distribution(
    report: Path, *, recipe: Path, tag: str, published: bool
) -> RecipeDistribution:
    """Reject stale, partial or incompatible reports before any deployment."""
    try:
        data = _object(json.loads(report.read_text(encoding="utf-8")), "report")
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read recipe distribution {report}") from error
    if type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 2:
        raise ValueError("Unsupported recipe distribution schemaVersion")
    recipe_data = _object(data.get("recipe"), "recipe")
    expected_sha = hashlib.sha256(recipe.read_bytes()).hexdigest()
    if recipe_data.get("sha256") != expected_sha:
        raise ValueError("Recipe distribution hash does not match profile")
    if data.get("tag") != tag:
        raise ValueError("Recipe distribution tag does not match run")
    modules = data.get("modules")
    if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
        raise ValueError("Recipe distribution modules are invalid")
    rows = data.get("components")
    if not isinstance(rows, list):
        raise ValueError("Recipe distribution components are invalid")
    components: list[RecipeComponent] = []
    keys: set[tuple[str, str, str]] = set()
    references: set[str] = set()
    for index, row in enumerate(rows):
        item = _object(row, f"components[{index}]")
        kind = _string(item.get("kind"), "component kind")
        name = _string(item.get("name"), "component name")
        sdk = _string(item.get("sdk"), "component sdk")
        key = (kind, name, sdk)
        if key in keys:
            raise ValueError(f"Duplicate recipe component {key}")
        keys.add(key)
        image = _object(item.get("image"), f"{name} image")
        if "platforms" in image:
            raise ValueError("Multi-platform recipe distributions are not supported")
        reference = _string(image.get("reference"), f"{name} image reference")
        if reference in references:
            raise ValueError(f"Duplicate recipe image {reference}")
        references.add(reference)
        image_id = _string(image.get("id"), f"{name} image id")
        status = "published" if published else "built"
        if image.get("status") != status:
            raise ValueError(f"{name} image status must be {status}")
        digest = image.get("digest")
        if published:
            digest = _string(digest, f"{name} image digest")
        elif digest is not None:
            raise ValueError(f"{name} built image cannot have a published digest")
        components.append(
            RecipeComponent(
                kind=kind,
                name=name,
                sdk=sdk,
                mode=item.get("mode"),
                image=RecipeImage(reference, image_id, status, digest),
                variant=item.get("variant"),
                optimization=item.get("optimization"),
                native=_object(item["native"], "native") if "native" in item else None,
            )
        )
    source = data.get("source")
    if source is not None:
        source = _object(source, "source")
    _verify_recipe_identity(recipe, tag, modules, components)
    return RecipeDistribution(
        report=report,
        recipe_sha256=expected_sha,
        tag=tag,
        source=source,
        modules=tuple(modules),
        components=tuple(components),
    )


class _RecipeTask(Task[RecipeDistribution]):
    target: Literal["assembleRecipe", "publishRecipe"]
    published: bool

    def __init__(self, run: RecipeRun, *, executor: CommandTaskExecutor) -> None:
        self.run_config = run
        self.executor = executor
        self.title = f"{self.target} {run.recipe.name}"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[RecipeDistribution]:
        run = self.run_config
        report = run.output_dir / "distribution.json"
        report.unlink(missing_ok=True)
        command = CommandTask(
            argv=recipe_command(
                self.target,
                recipe=str(run.recipe),
                output=str(run.output_dir),
                tag=run.tag,
            ),
            executor=self.executor,
            role="host",
            options=CommandOptions(cwd=run.source_dir),
            title=self.title,
        )
        log = run.output_dir.parent / "gradle.log"
        try:
            result = command.run(inputs).value
        except RuntimeError as error:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(str(error) + "\n")
            raise
        if result is None:
            raise RuntimeError(f"{self.target} produced no command result")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(result.stdout + "\n" + result.stderr)
        return TaskOutcome(
            value=read_distribution(
                report,
                recipe=run.recipe,
                tag=run.tag,
                published=self.published,
            )
        )

    @override
    def _fingerprint_payload(self) -> object:
        run = self.run_config
        return {"target": self.target, "recipe": str(run.recipe), "tag": run.tag}


class AssembleRecipeTask(_RecipeTask):
    """Build one recipe and read its local distribution."""

    target = "assembleRecipe"
    published = False


class PublishRecipeTask(_RecipeTask):
    """Build and publish one recipe, then require verified digests."""

    target = "publishRecipe"
    published = True


def recipe_command(
    target: Literal["assembleRecipe", "publishRecipe"],
    *,
    recipe: str,
    output: str,
    tag: str,
    containerd_maven_repository: Path | None = None,
    native_build_memory: str | None = None,
    native_parallelism: int | None = None,
) -> tuple[str, ...]:
    """Build a recipe with the selected backend dependencies."""
    return (
        "./gradlew",
        target,
        f"-Precipe={recipe}",
        f"-PrecipeTag={tag}",
        f"-PrecipeOutput={output}",
        *(
            (
                "-PcontainerdMavenLocal=true",
                f"-Dmaven.repo.local={containerd_maven_repository}",
            )
            if containerd_maven_repository is not None
            else ()
        ),
        *(
            (f"-PnativeBuildMemory={native_build_memory}",)
            if native_build_memory is not None
            else ()
        ),
        *(
            (f"-PnativeParallelism={native_parallelism}",)
            if native_parallelism is not None
            else ()
        ),
        "--no-daemon",
    )


@dataclass(frozen=True, slots=True)
class RecipeBinding:
    """A published distribution and the selected runtime consumers."""

    distribution: Resource[RecipeDistribution]
    functions: dict[str, tuple[str, str]]
    run_dir: Path
    project: DockerComposeProject | None = None
    target: Resource[Any] | None = None
    remote_root: PurePosixPath | None = None


def require_validation_distribution(
    distribution: RecipeDistribution,
    *,
    functions: tuple[tuple[str, str], ...],
    required_modules: frozenset[str] = frozenset(
        {"build-metadata", "container-deployment-provider"}
    ),
    exact_modules: bool = False,
) -> None:
    """Require the exact components and local registry used by container validation."""
    control_planes = [
        component
        for component in distribution.components
        if component.kind == "control-plane"
    ]
    selected = {
        (component.name, component.sdk)
        for component in distribution.components
        if component.kind == "function"
    }
    expected = {(name, "bash" if sdk == "exec" else sdk) for name, sdk in functions}
    if selected != expected or len(selected) != len(functions):
        raise ValueError("Recipe distribution differs from selected functions")
    if len(control_planes) != 1 or any(
        component.kind == "service" for component in distribution.components
    ):
        raise ValueError("Recipe distribution needs one control plane and no services")
    if not required_modules.issubset(distribution.modules):
        raise ValueError("Recipe distribution lacks required control-plane modules")
    if exact_modules and set(distribution.modules) != required_modules:
        raise ValueError("Recipe distribution has unexpected control-plane modules")
    if any(
        not component.image.reference.startswith(f"{LOCAL_REGISTRY}/")
        for component in distribution.components
    ):
        raise ValueError("Recipe distribution images must use the local registry")


def recipe_distribution_resource(
    *,
    source: Path,
    recipe: Path,
    run_dir: Path,
    tag: str,
    executor: CommandTaskExecutor,
    functions: tuple[tuple[str, str], ...],
    requires: tuple[Resource[Any], ...] = (),
    required_modules: frozenset[str] = frozenset(
        {"build-metadata", "container-deployment-provider"}
    ),
    exact_modules: bool = False,
) -> Resource[RecipeDistribution]:
    """Publish a recipe after prerequisites are acquired and retain its report."""

    def acquire(inputs: TaskInputs) -> RecipeDistribution:
        run = prepare_recipe_run(source, recipe, run_dir, tag)
        result = PublishRecipeTask(run, executor=executor).run(inputs).value
        if result is None:
            raise RuntimeError("publishRecipe produced no distribution")
        require_validation_distribution(
            result,
            functions=functions,
            required_modules=required_modules,
            exact_modules=exact_modules,
        )
        return result

    return Resource(
        title=f"Publish recipe {recipe.name}",
        acquire=acquire,
        release=lambda _inputs, _value: None,
        requires=requires,
    )


def recipe_run_resource(
    *,
    source: Path,
    recipe: Path,
    run_dir: Path,
    tag: str,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[RecipeRun]:
    """Capture the selected source only when the workflow acquires it."""
    return Resource(
        title=f"Stage recipe {recipe.name}",
        acquire=lambda _inputs: prepare_recipe_run(source, recipe, run_dir, tag),
        release=lambda _inputs, _value: None,
        requires=requires,
    )


def assembled_recipe_distribution_resource(
    *,
    run: Resource[RecipeRun],
    executor: CommandTaskExecutor,
    functions: tuple[tuple[str, str], ...],
    required_modules: frozenset[str],
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[RecipeDistribution]:
    """Build a staged recipe and expose its checked local distribution."""

    def acquire(inputs: TaskInputs) -> RecipeDistribution:
        staged = inputs.resource(run)
        result = AssembleRecipeTask(staged, executor=executor).run(inputs).value
        if result is None:
            raise RuntimeError("assembleRecipe produced no distribution")
        require_validation_distribution(
            result, functions=functions, required_modules=required_modules
        )
        return result

    return Resource(
        title="Assemble staged recipe",
        acquire=acquire,
        release=lambda _inputs, _value: None,
        requires=(run, *requires),
    )
