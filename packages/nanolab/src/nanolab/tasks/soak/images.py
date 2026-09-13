"""Prepare single-version build recipes using NanoLab's existing image catalogue."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from nanolab.config.soak import ImageBuildSpec
from nanolab.functions.catalog import resolve_function_definition
from nanolab.images.bake import render_bake
from nanolab.images.control_plane_variants import (
    DEFAULT_JVM_TUNING,
    jvm_optimization,
    resolve_variants,
)
from nanolab.images.plan import (
    ImageArchitecture,
    ImageCell,
    ImageTarget,
    build_image_plan,
)
from nanolab.tasks.soak.artifacts import describe_artifact, fingerprint

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REFERENCE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class BuildRecipe:
    """One role's recipe, keeping execution separate from side-effect-free planning."""

    role: str
    mode: str
    variant: str
    platform: str
    image: str
    prerequisite_argv: tuple[str, ...] | None
    bake: dict[str, Any] | None
    recipe_fingerprint: str
    provenance_receipt: str | None


@dataclass(frozen=True)
class BuildReceipt:
    """Identify actual outputs and evidence, not an image tag inferred from intent."""

    role: str
    image_digest: str
    source_fingerprint: str
    recipe_fingerprint: str
    build_fingerprint: str
    platform: str
    toolchains: tuple[tuple[str, str], ...]
    base_images: tuple[tuple[str, str], ...]
    logs: tuple[dict[str, object], ...]
    # Build observation instruments the recipe before building it, so
    # recipe_fingerprint above identifies what was built and this identifies
    # what the scenario asked for. None when nothing rewrote the recipe.
    original_recipe_fingerprint: str | None = None


def build_key(source: str, recipe: str, platform: str) -> str:
    """Bind a build identity to source, recipe and architecture separately."""
    if not source or not recipe or not platform:
        raise ValueError("build identity requires source, recipe and platform")
    return fingerprint({"source": source, "recipe": recipe, "platform": platform})


def _target_for(role: str, root: Path, targets: tuple[ImageTarget, ...]) -> ImageTarget:
    if role == "control-plane":
        return next(target for target in targets if target.name == "control-plane")
    if role == "proxy":
        raise ValueError("proxy requires an explicitly supported catalogue recipe")
    definition = resolve_function_definition(role, root)
    if definition.example_dir is None:
        raise ValueError(f"function {role} has no buildable example")
    dockerfile = definition.example_dir.relative_to(root) / "Dockerfile"
    matches = [target for target in targets if target.dockerfile == dockerfile]
    if len(matches) != 1:
        raise ValueError(f"function {role} must resolve to exactly one image target")
    return matches[0]


def _java_recipe(
    role: str, spec: ImageBuildSpec, cell: ImageCell
) -> tuple[dict[str, str], tuple[str, ...] | None]:
    variant = resolve_variants((spec.variant,))[0]
    native = cell.native_build
    expected_native = variant.key.startswith("native-")
    if expected_native != (native is not None):
        raise ValueError(f"runtime and variant disagree for {role}")
    modules = ",".join(spec.modules) or "none"
    if native is None:
        prerequisite = list(cell.target.jvm_prerequisite_arguments)
        if role == "control-plane":
            prerequisite = [
                arg
                for arg in prerequisite
                if not arg.startswith("-PcontrolPlaneModules=")
            ]
            prerequisite.extend(
                (
                    f"-PcontrolPlaneModules={modules}",
                    "-PnanofaasBuildType=jvm",
                    f"-PnanofaasBuildVariant={variant.key}",
                    f"-PnanofaasBuildOptimization={jvm_optimization(variant)}",
                )
            )
        return (
            {"JVM_TUNING": variant.build_env.get("JVM_TUNING", DEFAULT_JVM_TUNING)},
            ("./gradlew", *prerequisite, "--no-daemon"),
        )
    gradle_args = list(native.gradle_args)
    if role == "control-plane":
        gradle_args = [
            arg for arg in gradle_args if not arg.startswith("-PcontrolPlaneModules=")
        ]
        gradle_args.extend(
            (
                f"-PcontrolPlaneModules={modules}",
                "-PnanofaasBuildType=native",
                f"-PnanofaasBuildVariant={variant.key}",
                f"-PnanofaasBuildOptimization={variant.build_env['NATIVE_OPTIMIZATION']}",
            )
        )
    gradle_args.append(
        f"-PnativeOptimization={variant.build_env['NATIVE_OPTIMIZATION']}"
    )
    gc = variant.build_env.get("NATIVE_GC")
    if gc is not None:
        gradle_args.append(f"-PnativeGc={gc}")
    return (
        {
            "NATIVE_TASK": native.task,
            "NATIVE_BINARY": native.binary.as_posix(),
            "GRADLE_ARGS": " ".join(gradle_args),
            "GRAALVM_DISTRIBUTION": "oracle" if gc == "G1" else "community",
        },
        None,
    )


def plan_images(
    source_root: Path,
    images: Mapping[str, ImageBuildSpec],
    runtimes: Mapping[str, str],
    *,
    registry: str,
    run_id: str,
) -> tuple[BuildRecipe, ...]:
    """Resolve role variants without invoking Docker or rebuilding an image."""
    if set(images) != set(runtimes):
        raise ValueError("image and runtime roles must match")
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,47}", run_id) is None:
        raise ValueError("run ID must be safe for an image tag")
    registry = registry.rstrip("/")
    if not registry or re.search(r"\s|@|://", registry) or registry.startswith("/"):
        raise ValueError("registry must be an image repository prefix, not a URL")
    root = source_root.resolve()
    recipes = []
    for role, spec in images.items():
        runtime = runtimes[role]
        if runtime not in {"jvm", "native", "node"}:
            raise ValueError(f"unsupported runtime for {role}: {runtime}")
        if runtime == "node":
            if spec.variant != "default":
                raise ValueError(
                    f"runtime node requires the default catalogue flavor: {role}"
                )
        else:
            variant = resolve_variants((spec.variant,))[0]
            if variant.key.startswith("native-") != (runtime == "native"):
                raise ValueError(f"runtime and variant disagree for {role}")
        if spec.modules and role != "control-plane":
            raise ValueError("control-plane modules cannot be applied to an SDK image")
        unsupported = set(spec.build_options) - {"BUILDER_IMAGE", "RUNTIME_IMAGE"}
        if unsupported:
            raise ValueError(
                "unsupported build options: " + ", ".join(sorted(unsupported))
            )
        if spec.mode == "prebuilt":
            if spec.digest is None:
                raise ValueError("prebuilt requires a digest")
            recipes.append(
                BuildRecipe(
                    role,
                    spec.mode,
                    spec.variant,
                    spec.platform,
                    spec.digest,
                    None,
                    None,
                    fingerprint(spec.model_dump(mode="json")),
                    spec.provenance_receipt,
                )
            )
            continue
        architecture = cast(ImageArchitecture, spec.platform.split("/")[1])
        # Catalogue tags are placeholders, replaced below with run-specific tags.
        catalogue = build_image_plan(
            root, "0.0.0", registry=registry, architectures=(architecture,)
        )
        target = _target_for(role, root, catalogue.targets)
        flavor = "default" if runtime == "node" else runtime
        matches = [
            cell
            for cell in catalogue.cells
            if cell.target == target and cell.flavor == flavor
        ]
        if len(matches) != 1:
            raise ValueError(f"catalogue does not support runtime {runtime} for {role}")
        tag = f"soak-{run_id}-{architecture}"
        cell = replace(matches[0], tag=tag, image=f"{registry}/{target.name}:{tag}")
        bake = render_bake(replace(catalogue, targets=(target,), cells=(cell,)))
        rendered = next(iter(bake["target"].values()))
        rendered["platforms"] = [spec.platform]
        if runtime == "node":
            args, prerequisite = {}, None
        else:
            args, prerequisite = _java_recipe(role, spec, cell)
        args.update(spec.build_options)
        rendered["args"] = args
        recipe_identity = fingerprint(
            {
                "role": role,
                "variant": spec.variant,
                "platform": spec.platform,
                "prerequisite": list(prerequisite) if prerequisite else None,
                "build": {
                    key: value for key, value in rendered.items() if key != "tags"
                },
            }
        )
        recipes.append(
            BuildRecipe(
                role,
                spec.mode,
                spec.variant,
                spec.platform,
                cell.image,
                prerequisite,
                bake,
                recipe_identity,
                None,
            )
        )
    return tuple(recipes)


def freeze_build_receipt(
    recipe: BuildRecipe,
    source_fingerprint: str,
    observed_digest: str,
    *,
    toolchains: Mapping[str, str],
    base_images: Mapping[str, str],
    logs: tuple[Path, ...],
) -> BuildReceipt:
    """Freeze the collector's actual output; never infer provenance from a tag."""
    if _DIGEST.fullmatch(observed_digest) is None:
        raise ValueError("observed image digest is not a SHA-256 digest")
    if not source_fingerprint or not toolchains or not base_images or not logs:
        raise ValueError(
            "build provenance requires source, toolchains, base images and logs"
        )
    if any(not value.strip() for value in toolchains.values()) or any(
        _REFERENCE.fullmatch(value) is None for value in base_images.values()
    ):
        raise ValueError(
            "build provenance must identify effective toolchains and base digests"
        )
    if recipe.mode == "prebuilt":
        if recipe.image.rsplit("@", 1)[-1] != observed_digest:
            raise ValueError("prebuilt image digest differs from the requested digest")
        image = recipe.image
    else:
        image = recipe.image.rsplit(":", 1)[0] + "@" + observed_digest
    evidence = tuple(describe_artifact(path) for path in logs)
    return BuildReceipt(
        recipe.role,
        image,
        source_fingerprint,
        recipe.recipe_fingerprint,
        build_key(source_fingerprint, recipe.recipe_fingerprint, recipe.platform),
        recipe.platform,
        tuple(sorted(toolchains.items())),
        tuple(sorted(base_images.items())),
        evidence,
    )
