"""Reusable recipe profiles and the prepared comparison handoff."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from nanolab.images.control_plane_variants import VARIANTS_BY_KEY
from nanolab.tasks.recipe import RecipeDistribution

COMPARISON_FUNCTIONS = {
    "word-stats-java": ("word-stats", "java"),
    "word-stats-javascript": ("word-stats", "javascript"),
}
COMPARISON_RECIPE_MODULES = frozenset(
    {"k8s-deployment-provider", "async-queue", "build-metadata"}
)
COMPARISON_SCHEDULER_STRATEGY = "per-function"


@dataclass(frozen=True)
class PreparedComparison:
    """Validated distributions sharing a captured remote source."""

    remote_source: PurePosixPath
    distributions: Mapping[str, RecipeDistribution]


def comparison_profiles(tool_root: Path, variants: tuple[str, ...]) -> dict[str, Path]:
    """Resolve selected profiles, always preparing shared JVM functions first."""
    if not variants or len(set(variants)) != len(variants):
        raise ValueError("comparison variants must be nonempty and unique")
    if set(variants) - VARIANTS_BY_KEY.keys():
        raise ValueError("unknown comparison variants")
    profiles = {
        key: tool_root / "recipes" / f"comparison-{key}.yaml"
        for key in dict.fromkeys(("jvm", *variants))
    }
    for profile in profiles.values():
        declared_options(profile)
    return profiles


def declared_options(profile: Path) -> dict[str, object]:
    """Read effective build options and reject scheduler contract conflicts."""
    recipe = yaml.safe_load(profile.read_text())
    cp = recipe["controlPlane"]
    modules = cp["modules"]
    if set(modules) != COMPARISON_RECIPE_MODULES or len(modules) != 3:
        raise ValueError("comparison recipe requires the exact comparison modules")
    config = cp.get("config", {}).get("nanofaas", {})
    strategy = config.get("scheduler", {}).get("strategy", "per-function")
    admin = config.get("admin", {}).get("runtime-config", {}).get("enabled", False)
    alternate_admin = (
        config.get("runtime-config", {}).get("admin", {}).get("enabled", False)
    )
    if strategy != COMPARISON_SCHEDULER_STRATEGY or admin or alternate_admin:
        raise ValueError(
            "comparison scheduler strategy and admin configuration conflict"
        )
    build = cp["build"]
    native = build.get("native", {})
    monitoring = list(native.get("monitoring", []))
    gc = native.get("gc", "serial")
    if gc == "G1" and "jfr" not in monitoring:
        monitoring.append("jfr")
    return {
        "mode": build["mode"],
        "variant": build["variant"],
        "modules": sorted(modules),
        "jvm_args": cp.get("jvm", {}).get("args", []),
        "native": {
            "builder": build.get("builder", "host"),
            "optimization": str(native.get("optimization", "3")),
            "gc": gc,
            "monitoring": sorted(monitoring),
        }
        if build["mode"] == "native"
        else None,
        "functions": [
            {
                "name": function["name"],
                "sdk": function["sdk"],
                "mode": function.get("build", {}).get("mode"),
                "image": function["container"]["image"],
            }
            for function in recipe.get("functions", [])
        ],
    }
