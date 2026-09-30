"""Multiarch publication evidence, separate from local image distributions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sonata_engine import Resource, TaskInputs
from sonata_tasks.docker import DockerTask
from sonata_tasks.execution.ports import CommandTaskExecutor

from nanolab.tasks.recipe import (
    RecipeComponent,
    RecipeDistribution,
    RecipeImage,
    _object,
    _string,
    _verify_recipe_identity,
    execute_recipe,
    require_validation_distribution,
)
from nanolab.tasks.recipe_builder import RecipeBuilder
from nanolab.workspace.recipe import prepare_recipe_run

MULTIARCH_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})


def sha256_digest(value: object) -> str:
    """Require a complete lowercase SHA-256 registry identity."""
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Malformed sha256 digest")
    return value


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse JSON keys that would otherwise silently overwrite evidence."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class MultiarchImage:
    """Published index and its executable platform manifests."""

    reference: str
    status: str
    digest: str
    platforms: tuple[str, ...]
    manifests: dict[str, str]
    provenance: bool


@dataclass(frozen=True, slots=True)
class MultiarchComponent:
    """Component metadata with no local image identity."""

    kind: str
    name: str
    sdk: str
    mode: str | None
    image: MultiarchImage
    variant: str | None
    optimization: str | None
    native: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class MultiarchDistribution:
    """Checked publication before registry verification and host projection."""

    report: Path
    recipe_sha256: str
    tag: str
    source: dict[str, object]
    modules: tuple[str, ...]
    components: tuple[MultiarchComponent, ...]


def read_multiarch_distribution(
    report: Path, *, recipe: Path, tag: str, expected_source: dict[str, object]
) -> MultiarchDistribution:
    """Validate publication identity without requiring or inventing local IDs."""
    try:
        data = _object(
            json.loads(report.read_bytes(), object_pairs_hook=unique_json_object),
            "report",
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read multiarch distribution {report}") from error
    profile = _object(yaml.safe_load(recipe.read_text()), "profile")
    registry = _object(profile.get("registry"), "registry")
    platforms = registry.get("platforms")
    if (
        not isinstance(platforms, list)
        or len(platforms) != 2
        or set(platforms) != MULTIARCH_PLATFORMS
    ):
        raise ValueError("Multiarch recipe requires linux/amd64 and linux/arm64")
    if registry.get("provenance", False) is not False:
        raise ValueError("Multiarch recipe provenance is not supported")
    if type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 2:
        raise ValueError("Unsupported multiarch distribution schemaVersion")
    expected_sha = hashlib.sha256(recipe.read_bytes()).hexdigest()
    if (
        _object(data.get("recipe"), "recipe").get("sha256") != expected_sha
        or data.get("tag") != tag
    ):
        raise ValueError("Multiarch recipe hash or tag differs from run")
    source = _object(data.get("source"), "source")
    if type(source.get("dirty")) is not bool or any(
        source.get(key) != expected_source.get(key) for key in ("revision", "dirty")
    ):
        raise ValueError("Multiarch source differs from captured inputs")
    _string(source.get("revision"), "source revision")
    modules = data.get("modules")
    if not isinstance(modules, list) or not all(
        isinstance(item, str) for item in modules
    ):
        raise ValueError("Multiarch modules are invalid")
    rows = data.get("components")
    if not isinstance(rows, list):
        raise ValueError("Multiarch components are invalid")
    components: list[MultiarchComponent] = []
    keys: set[tuple[str, str, str]] = set()
    references: set[str] = set()
    for row in rows:
        item = _object(row, "component")
        key = (
            _string(item.get("kind"), "kind"),
            _string(item.get("name"), "name"),
            _string(item.get("sdk"), "sdk"),
        )
        image = _object(item.get("image"), "image")
        reference = _string(image.get("reference"), "image reference")
        if key in keys or reference in references:
            raise ValueError("Duplicate multiarch component or image reference")
        keys.add(key)
        references.add(reference)
        selected = image.get("platforms")
        if (
            not isinstance(selected, list)
            or len(selected) != 2
            or set(selected) != set(platforms)
        ):
            raise ValueError("Multiarch image platforms differ from recipe")
        manifests = _object(image.get("manifests"), "image manifests")
        if set(manifests) != set(platforms):
            raise ValueError("Multiarch manifest platforms differ from recipe")
        if (
            image.get("status") != "published"
            or image.get("provenance") is not False
            or "id" in image
        ):
            raise ValueError(
                "Multiarch image must be published without provenance or local ID"
            )
        components.append(
            MultiarchComponent(
                kind=key[0],
                name=key[1],
                sdk=key[2],
                mode=item.get("mode"),
                image=MultiarchImage(
                    reference,
                    "published",
                    sha256_digest(image.get("digest")),
                    tuple(selected),
                    {
                        platform: sha256_digest(digest)
                        for platform, digest in manifests.items()
                    },
                    False,
                ),
                variant=item.get("variant"),
                optimization=item.get("optimization"),
                native=_object(item["native"], "native") if "native" in item else None,
            )
        )
    _verify_recipe_identity(recipe, tag, modules, components)
    return MultiarchDistribution(
        report, expected_sha, tag, source, tuple(modules), tuple(components)
    )


def project_host_distribution(
    distribution: MultiarchDistribution,
    *,
    platform: str,
    configs: dict[str, dict[str, str]],
) -> RecipeDistribution:
    """Adapt verified host artifacts to existing runtime consumers."""
    if platform not in MULTIARCH_PLATFORMS:
        raise ValueError("Unsupported host platform")
    components = []
    for component in distribution.components:
        image = component.image
        if image.reference not in configs or set(configs[image.reference]) != set(
            image.platforms
        ):
            raise ValueError("Incomplete verified platform configuration map")
        manifest = sha256_digest(image.manifests[platform])
        config = sha256_digest(configs[image.reference][platform])
        components.append(
            RecipeComponent(
                component.kind,
                component.name,
                component.sdk,
                component.mode,
                RecipeImage(
                    image.reference.rsplit(":", 1)[0] + "@" + manifest,
                    config,
                    "published",
                    manifest,
                ),
                component.variant,
                component.optimization,
                component.native,
            )
        )
    return RecipeDistribution(
        distribution.report,
        distribution.recipe_sha256,
        distribution.tag,
        distribution.source,
        distribution.modules,
        tuple(components),
    )


def multiarch_recipe_distribution_resource(
    *,
    source: Path,
    recipe: Path,
    run_dir: Path,
    tag: str,
    executor: CommandTaskExecutor,
    functions: tuple[tuple[str, str], ...],
    builder: Resource[RecipeBuilder],
) -> Resource[RecipeDistribution]:
    """Publish once, prove registry identity, then expose only verified host images."""

    def acquire(inputs: TaskInputs) -> RecipeDistribution:
        from nanolab.tasks.recipe_registry import (
            fetch_local_registry,
            verify_multiarch_registry,
        )

        mapping = run_dir / "runtime-images.json"
        mapping.unlink(missing_ok=True)
        run = prepare_recipe_run(source, recipe, run_dir / "recipe", tag)
        captured = json.loads((run_dir / "recipe/recipe-inputs.json").read_bytes())
        selected_builder = inputs.resource(builder)
        report = execute_recipe(
            run,
            target="publishRecipe",
            executor=executor,
            inputs=inputs,
            builder=selected_builder.name,
        )
        publication = read_multiarch_distribution(
            report,
            recipe=run.recipe,
            tag=tag,
            expected_source={
                "revision": captured["revision"],
                "dirty": captured["patchSha256"] != hashlib.sha256(b"").hexdigest(),
            },
        )
        if (
            set(publication.modules)
            != {"build-metadata", "container-deployment-provider"}
            or {
                (item.kind, item.name, item.sdk, item.mode)
                for item in publication.components
            }
            != {
                ("control-plane", "control-plane", "java", "jvm"),
                ("function", "word-stats", "java", "jvm"),
            }
            or functions != (("word-stats", "java"),)
        ):
            raise ValueError("Unsupported multiarch validation components or modules")
        configs = verify_multiarch_registry(
            publication, fetch=fetch_local_registry, evidence_dir=run_dir / "registry"
        )
        runtime = project_host_distribution(
            publication, platform=selected_builder.platform, configs=configs
        )
        require_validation_distribution(
            runtime, functions=functions, exact_modules=True
        )
        images = []
        for component, published in zip(
            runtime.components, publication.components, strict=True
        ):
            _ = DockerTask(
                "pull",
                component.image.reference,
                executor=executor,
                role="host",
                title=f"Pull verified recipe {component.name}",
            ).run(inputs)
            images.append(
                {
                    "kind": component.kind,
                    "name": component.name,
                    "sdk": component.sdk,
                    "reference": component.image.reference,
                    "configurationDigest": component.image.id,
                    "indexDigest": published.image.digest,
                    "manifests": published.image.manifests,
                }
            )
        temporary = mapping.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"platform": selected_builder.platform, "images": images}, indent=2
            )
            + "\n"
        )
        temporary.replace(mapping)
        return runtime

    return Resource(
        title=f"Publish recipe {recipe.name} for both platforms",
        acquire=acquire,
        release=lambda _inputs, _value: None,
        requires=(builder,),
    )
