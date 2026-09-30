"""Recipe inputs and staging that preserve the soak source snapshot."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

from nanolab.config.soak import SoakConfig
from nanolab.tasks.recipe import _object
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
        or config.purpose != "smoke"
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
        raise ValueError("Unsupported container smoke recipe selection")
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
    snapshot: SourceSnapshot, profile: Path, run_dir: Path, tag: str
) -> RecipeRun:
    """Materialize all captured files; staging Git identity is not source identity."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,47}", tag):
        raise ValueError("Unsafe soak recipe tag")
    verify_snapshot(snapshot)
    profile_bytes = profile.read_bytes()
    run_dir.mkdir(parents=True, exist_ok=False)
    source = materialize_snapshot(snapshot, run_dir / "source")
    staged_profile = run_dir / "recipe.yaml"
    staged_profile.write_bytes(profile_bytes)
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
    (run_dir / "source-identity.json").write_text(
        json.dumps(
            {
                "revision": snapshot.revision,
                "dirty": snapshot.dirty,
                "fingerprint": snapshot.fingerprint,
                "staging_revision": staging_revision,
                "recipe_sha256": hashlib.sha256(profile_bytes).hexdigest(),
                "tag": tag,
            },
            indent=2,
        )
        + "\n"
    )
    return RecipeRun(source, staged_profile, run_dir / "distribution", tag)
