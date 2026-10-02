"""Recipe inputs and staging that preserve the soak source snapshot."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import yaml

from nanolab.config.soak import SoakConfig
from nanolab.tasks.recipe import _object
from nanolab.tasks.recipe_registry import fetch_local_registry
from nanolab.tasks.soak.artifacts import (
    ArtifactWriter,
    describe_artifact,
    enforce_limit,
)
from nanolab.tasks.soak.build_executor import OwnedBuildCommandExecutor
from nanolab.tasks.soak.build_provenance import _materials, _predicate
from nanolab.tasks.soak.images import BuildReceipt, BuildRecipe, freeze_build_receipt
from nanolab.tasks.soak.recipe_observation import (
    observe_soak_recipe,
    verify_recipe_provenance,
    verify_recipe_source,
)
from nanolab.tasks.soak.recipe_registry import verify_soak_registry
from nanolab.tasks.soak.sources import (
    SourceSnapshot,
    materialize_snapshot,
    verify_snapshot,
)
from nanolab.workspace.recipe import RecipeRun

SOAK_RECIPE_MODULES = frozenset(
    {"container-deployment-provider", "async-queue", "build-metadata"}
)
SOAK_RECIPE_ROLES = {
    "control-plane": ("jvm", "jvm"),
    "word-stats-java": ("jvm", "jvm"),
    "word-stats-javascript": ("node", "default"),
}


def validate_soak_recipe(profile: Path, config: SoakConfig, *, platform: str) -> None:
    """Reject conflicting build policy before acquiring infrastructure."""
    data = _object(yaml.safe_load(profile.read_bytes()), "soak profile")
    registry = _object(data.get("registry"), "soak registry")
    control = _object(data.get("controlPlane"), "control plane")
    build = _object(control.get("build"), "control-plane build")
    modules = control.get("modules")
    if (
        type(data.get("schemaVersion")) is not int
        or data["schemaVersion"] != 2
        or config.purpose not in {"smoke", "p24"}
        or platform not in {"linux/amd64", "linux/arm64"}
        or registry.get("platforms") != [platform]
        or registry.get("repository") != "127.0.0.1:5000/nanofaas"
        or registry.get("provenance") is not True
        or data.get("services")
        or build.get("mode") != "jvm"
        or build.get("variant", "jvm") != "jvm"
        or not isinstance(modules, list)
        or not all(isinstance(item, str) for item in modules)
        or len(modules) != len(SOAK_RECIPE_MODULES)
        or set(modules) != SOAK_RECIPE_MODULES
        or set(config.roles) != set(SOAK_RECIPE_ROLES)
        or set(config.images) != set(SOAK_RECIPE_ROLES)
    ):
        raise ValueError("Unsupported container soak recipe selection")
    functions = data.get("functions")
    if not isinstance(functions, list) or len(functions) != 2:
        raise ValueError("Soak recipe requires Java and JavaScript word-stats")
    selected = {}
    for raw in functions:
        item = _object(raw, "soak function")
        key = (item.get("name"), item.get("sdk"))
        if key in selected:
            raise ValueError("Duplicate soak recipe function")
        selected[key] = item
    if set(selected) != {("word-stats", "java"), ("word-stats", "javascript")}:
        raise ValueError("Soak recipe functions differ from roles")
    java = _object(selected[("word-stats", "java")].get("build"), "Java build")
    if (
        java.get("mode") != "jvm"
        or selected[("word-stats", "javascript")].get("build") is not None
    ):
        raise ValueError("Soak recipe function build mode differs from roles")
    if config.purpose == "p24":
        blocks = (
            (data, {"schemaVersion", "name", "registry", "controlPlane", "functions"}),
            (registry, {"repository", "tag", "platforms", "provenance"}),
            (control, {"modules", "build", "container", "jvm"}),
            (build, {"mode", "variant"}),
        )
        if control.get("jvm") != {
            "args": ["-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1"]
        } or any(set(block) - allowed for block, allowed in blocks):
            raise ValueError("P24 recipe must preserve existing build settings")
        for component in (control, *selected.values()):
            allowed = (
                {"modules", "build", "container", "jvm"}
                if component is control
                else {"name", "sdk", "build", "container"}
            )
            container = _object(component.get("container"), "P24 container")
            component_build = _object(component.get("build", {}), "P24 build")
            if (
                set(component) - allowed
                or set(container) - {"image"}
                or set(component_build)
                - ({"mode", "variant"} if component is control else {"mode"})
            ):
                raise ValueError("P24 recipe must preserve existing build settings")
    for role, (runtime, variant) in SOAK_RECIPE_ROLES.items():
        spec = config.images[role]
        expected_modules = SOAK_RECIPE_MODULES if role == "control-plane" else set()
        if (
            config.roles[role].runtime != runtime
            or spec.variant != variant
            or spec.mode != "build"
            or spec.platform != platform
            or set(spec.modules) != expected_modules
            or spec.build_options
        ):
            raise ValueError(f"Soak recipe differs from image policy for {role}")


def prepare_soak_recipe_run(
    snapshot: SourceSnapshot,
    profile: Path,
    run_dir: Path,
    tag: str,
    *,
    artifact_limit_bytes: int = 16 * 1024 * 1024,
    budget_root: Path | None = None,
) -> RecipeRun:
    """Materialize all captured files; staging Git identity is not source identity."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,47}", tag):
        raise ValueError("Unsafe soak recipe tag")
    verify_snapshot(snapshot)
    profile_bytes = profile.read_bytes()
    run_dir.mkdir(parents=True, exist_ok=False)
    writer = ArtifactWriter(run_dir, artifact_limit_bytes, budget_root=budget_root)
    source = materialize_snapshot(snapshot, run_dir / "workspace-recipe")
    staged_profile = writer.write_file("recipe.yaml", profile_bytes)
    for argv in (
        ("git", "init", "-q"),
        ("git", "add", "-f", "."),
        (
            "git",
            "-c",
            "user.name=NanoLab snapshot",
            "-c",
            "user.email=snapshot@localhost",
            "commit",
            "--allow-empty",
            "-qm",
            "Materialize immutable soak snapshot",
        ),
    ):
        subprocess.run(argv, cwd=source, check=True, capture_output=True, timeout=60)
    staging_revision = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    verify_snapshot(snapshot)
    if profile.read_bytes() != profile_bytes:
        raise ValueError("Soak recipe changed during staging")
    writer.write_json(
        "source-identity.json",
        {
            "revision": snapshot.revision,
            "dirty": snapshot.dirty,
            "fingerprint": snapshot.fingerprint,
            "staging_revision": staging_revision,
            "recipe_sha256": hashlib.sha256(profile_bytes).hexdigest(),
            "tag": tag,
        },
    )
    writer.close()
    return RecipeRun(source, staged_profile, run_dir / "distribution", tag)


def publish_soak_recipe(
    snapshot: SourceSnapshot,
    profile: Path,
    config: SoakConfig,
    *,
    run_dir: Path,
    tag: str,
    builder: str,
    executor: OwnedBuildCommandExecutor,
    artifact_limit_bytes: int,
) -> tuple[BuildReceipt, ...]:
    """Publish once, verify every role, then freeze executable image receipts."""
    platform = config.images["control-plane"].platform
    validate_soak_recipe(profile, config, platform=platform)
    writer = ArtifactWriter(run_dir, artifact_limit_bytes, budget_root=run_dir)
    try:
        run = prepare_soak_recipe_run(
            snapshot,
            profile,
            run_dir / "recipe",
            tag,
            artifact_limit_bytes=artifact_limit_bytes,
            budget_root=run_dir,
        )
        publication, observed = observe_soak_recipe(
            run,
            snapshot,
            executor=executor,
            builder=builder,
            artifact_limit_bytes=artifact_limit_bytes,
            budget_root=run_dir,
        )
        verified = verify_soak_registry(
            publication,
            platform=platform,
            evidence_dir=run_dir / "registry",
            fetch=fetch_local_registry,
            artifact_limit_bytes=artifact_limit_bytes,
            budget_root=run_dir,
        )
        if set(verified) != set(config.roles) or set(observed) != set(config.roles):
            raise ValueError("Recipe publication does not cover all soak roles")
        request = json.loads(
            (run.recipe.parent / "observer/publication-request.json").read_bytes()
        )
        verify_recipe_source(
            snapshot,
            run.source_dir,
            generated_inputs=frozenset(
                Path(item["path"]).relative_to(run.source_dir).as_posix()
                for item in request["instrumentation"]
                if Path(item["path"]).is_relative_to(run.source_dir)
            ),
        )
        if profile.read_bytes() != run.recipe.read_bytes():
            raise ValueError("Recipe profile changed across publication")
        receipts = []
        identities = {}
        for role in config.roles:
            image, observation = verified[role], observed[role]
            if observation.digest != image.publication_digest:
                raise ValueError("Recipe observation image differs from registry")
            for statement in image.provenance:
                predicate = _predicate(statement, platform, image.manifest_digest)
                if _materials(predicate) != observation.base_images:
                    raise ValueError(
                        "Recipe observed base materials differ from registry"
                    )
            facts = _object(
                json.loads(
                    (
                        run.recipe.parent / "observer" / (role + "-observations.json")
                    ).read_bytes()
                ),
                "recipe observation",
            )
            metadata = json.loads(
                (
                    run.recipe.parent
                    / "observer"
                    / facts["publication"]["metadata"]["path"]
                ).read_bytes()
            )
            for statement in image.provenance:
                verify_recipe_provenance(
                    _predicate(
                        metadata["buildx.build.provenance"],
                        platform,
                        image.manifest_digest,
                    ),
                    _predicate(statement, platform, image.manifest_digest),
                )
            recipe = BuildRecipe(**facts["recipe"])
            if facts.get("source_fingerprint") != snapshot.fingerprint:
                raise ValueError("Recipe observation source differs from snapshot")
            logs = tuple(
                dict.fromkeys(
                    (
                        *observation.logs,
                        run.recipe,
                        run.output_dir / "distribution.json",
                        run.recipe.parent / "source-identity.json",
                        *sorted((run_dir / "registry" / role).glob("*.json")),
                    )
                )
            )
            receipt = freeze_build_receipt(
                recipe,
                snapshot.fingerprint,
                image.manifest_digest,
                toolchains=observation.toolchains,
                base_images=observation.base_images,
                logs=logs,
            )
            receipts.append(
                replace(receipt, original_recipe_fingerprint=facts["profile_sha256"])
            )
            identities[role] = {
                "publication_digest": image.publication_digest,
                "manifest_digest": image.manifest_digest,
                "config_digest": image.config_digest,
                "reference": receipt.image_digest,
            }
        enforce_limit(run_dir, artifact_limit_bytes)
        identity = describe_artifact(
            writer.write_json("runtime-images.json", identities)
        )
        receipts = [replace(item, logs=(*item.logs, identity)) for item in receipts]
        for index, receipt in enumerate(receipts):
            writer.write_json(
                f"build-{index}.json", {"schema": "nanolab-soak-v1", **asdict(receipt)}
            )
        writer.write_json(
            "frozen-images.json",
            {
                "schema": "nanolab-soak-v1",
                "source_fingerprint": snapshot.fingerprint,
                "images": {item.role: item.image_digest for item in receipts},
            },
        )
        enforce_limit(run_dir, artifact_limit_bytes)
        return tuple(receipts)
    finally:
        writer.close()
