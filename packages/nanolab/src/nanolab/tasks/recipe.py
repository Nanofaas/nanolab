"""NanoFaaS recipe build commands and their distribution contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.gradle import GradleTask

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
        """Find a selected function by recipe name and SDK."""
        return self._component("function", name, sdk)

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
            )
        )
    source = data.get("source")
    if source is not None:
        source = _object(source, "source")
    return RecipeDistribution(
        report=report,
        recipe_sha256=expected_sha,
        tag=tag,
        source=source,
        modules=tuple(modules),
        components=tuple(components),
    )


class _RecipeTask(Task[RecipeDistribution]):
    target: str
    published: bool

    def __init__(self, run: RecipeRun, *, executor: CommandTaskExecutor) -> None:
        self.run_config = run
        self.executor = executor
        self.title = f"{self.target} {run.recipe.name}"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[RecipeDistribution]:
        run = self.run_config
        command = GradleTask(
            self.target,
            executor=self.executor,
            role="host",
            properties={
                "recipe": str(run.recipe),
                "recipeTag": run.tag,
                "recipeOutput": str(run.output_dir),
            },
            options=CommandOptions(cwd=run.source_dir),
            title=self.title,
        )
        _ = command.run(inputs)
        return TaskOutcome(
            value=read_distribution(
                run.output_dir / "distribution.json",
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


def recipe_distribution_resource(
    *,
    source: Path,
    recipe: Path,
    run_dir: Path,
    tag: str,
    executor: CommandTaskExecutor,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[RecipeDistribution]:
    """Publish a recipe after prerequisites are acquired and retain its report."""

    def acquire(inputs: TaskInputs) -> RecipeDistribution:
        run = prepare_recipe_run(source, recipe, run_dir, tag)
        result = PublishRecipeTask(run, executor=executor).run(inputs).value
        if result is None:
            raise RuntimeError("publishRecipe produced no distribution")
        return result

    return Resource(
        title=f"Publish recipe {recipe.name}",
        acquire=acquire,
        release=lambda _inputs, _value: None,
        requires=requires,
    )
