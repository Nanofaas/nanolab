"""Reusable recipe profiles and the prepared comparison handoff."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from nanolab.config.experiment_01 import CONTROL_PLANE_MAX_HEAP_MIB
from nanolab.images.control_plane_variants import (
    EXPERIMENT_01_VARIANTS_BY_KEY,
    VARIANTS_BY_KEY,
    ControlPlaneVariant,
)
from nanolab.tasks.recipes.workflow import RecipeDistribution

COMPARISON_FUNCTIONS = {
    "word-stats-java": ("word-stats", "java"),
    "word-stats-javascript": ("word-stats", "javascript"),
}
COMPARISON_RECIPE_MODULES = frozenset(
    {"k8s-deployment-provider", "async-queue", "build-metadata"}
)
COMPARISON_SCHEDULER_STRATEGY = "per-function"

# Experiment 01 drives one function, built once and published by the first
# recipe; every later recipe reuses that function's digest.
EXPERIMENT_01_FUNCTIONS = {"qr-code-rust": ("qr-code", "rust")}
_HEAP = f"-Xmx{CONTROL_PLANE_MAX_HEAP_MIB}m"
# A native image takes its heap limit at build time (-R:MaxHeapSize).
_NATIVE_HEAP = f"{CONTROL_PLANE_MAX_HEAP_MIB}m"
_JVM_TIER_ARGS = {
    "interpreted": ["-Xint"],
    "c1": ["-XX:TieredStopAtLevel=1"],
    "tiered": [],
}


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


def experiment_01_profiles(
    tool_root: Path, variants: tuple[str, ...]
) -> dict[str, Path]:
    """Resolve experiment 01 recipes in the order the caller listed them.

    The first recipe is the one that publishes the shared function; it must
    therefore be the recipe that declares it.
    """
    if not variants or len(set(variants)) != len(variants):
        raise ValueError("experiment-01 variants must be nonempty and unique")
    unknown = set(variants) - EXPERIMENT_01_VARIANTS_BY_KEY.keys()
    if unknown:
        raise ValueError(
            "unknown experiment-01 variants: " + ", ".join(sorted(unknown))
        )
    shared = next(iter(EXPERIMENT_01_VARIANTS_BY_KEY))
    ordered = (shared, *(key for key in variants if key != shared))
    profiles = {key: tool_root / "recipes" / f"{key}.yaml" for key in ordered}
    for key, profile in profiles.items():
        experiment_01_options(
            profile, EXPERIMENT_01_VARIANTS_BY_KEY[key], shares=key == shared
        )
    return profiles


def experiment_01_options(
    profile: Path, variant: ControlPlaneVariant, *, shares: bool
) -> dict[str, object]:
    """Read a recipe and require exactly the build the variant declares.

    The flags a recipe requests prove only what was asked for; what the process
    actually ran with is verified on the image and pod, and recorded apart.
    """
    recipe = yaml.safe_load(profile.read_text())
    cp = recipe["controlPlane"]
    if sorted(cp["modules"]) != sorted(variant.modules) or len(cp["modules"]) != len(
        variant.modules
    ):
        raise ValueError(f"{variant.key} requires modules {list(variant.modules)}")
    build = cp["build"]
    if "variant" in build or cp.get("config"):
        raise ValueError(
            "experiment-01 recipes carry no variant label or extra configuration"
        )
    runtime = variant.runtime
    if runtime is None:
        raise ValueError(f"{variant.key} declares no runtime class")
    if runtime.startswith("native-"):
        native = build.get("native", {})
        if (
            build["mode"] != "native"
            or str(native.get("optimization", "3")) != runtime.removeprefix("native-o")
            or native.get("gc", "serial") != "serial"
            or native.get("maxHeap") != _NATIVE_HEAP
            or native.get("monitoring")
            or "jvm" in cp
        ):
            raise ValueError(
                f"{variant.key} must be a serial native -O build, "
                f"maxHeap {_NATIVE_HEAP}"
            )
    elif build["mode"] != "jvm" or cp.get("jvm", {}).get("args") != [
        "-XX:+UseSerialGC",
        _HEAP,
        *_JVM_TIER_ARGS[runtime],
    ]:
        raise ValueError(f"{variant.key} must run serial GC, {_HEAP} and its tier")
    functions = [
        (function["name"], function["sdk"]) for function in recipe.get("functions", [])
    ]
    expected = list(EXPERIMENT_01_FUNCTIONS.values()) if shares else []
    if functions != expected or recipe.get("services"):
        raise ValueError(
            "only the first experiment-01 recipe declares the shared function"
        )
    return {
        **declared_options(profile, modules=frozenset(variant.modules)),
        "runtime": runtime,
    }


def declared_options(
    profile: Path, *, modules: frozenset[str] = COMPARISON_RECIPE_MODULES
) -> dict[str, object]:
    """Read effective build options and reject scheduler contract conflicts."""
    recipe = yaml.safe_load(profile.read_text())
    cp = recipe["controlPlane"]
    selected = cp["modules"]
    if set(selected) != modules or len(selected) != len(modules):
        raise ValueError("comparison recipe requires the exact comparison modules")

    def settings(value: object, prefix: str = ""):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from settings(item, f"{prefix}.{key}")
        else:
            yield re.sub(r"[^a-z0-9]", "", prefix.lower()), value

    for key, value in settings(cp.get("config", {})):
        if (
            key == "nanofaasschedulerstrategy"
            and value != COMPARISON_SCHEDULER_STRATEGY
        ):
            raise ValueError("comparison scheduler strategy configuration conflicts")
        if (
            key
            in {
                "nanofaasadminruntimeconfigenabled",
                "nanofaasruntimeconfigadminenabled",
            }
            and value is not None
            and str(value).lower() not in {"false", "0"}
        ):
            raise ValueError("comparison scheduler admin configuration conflicts")
    build = cp["build"]
    native = build.get("native", {})
    monitoring = list(native.get("monitoring", []))
    gc = native.get("gc", "serial")
    if gc == "G1" and "jfr" not in monitoring:
        monitoring.append("jfr")
    return {
        "mode": build["mode"],
        "variant": build.get("variant"),
        "modules": sorted(selected),
        "jvm_args": cp.get("jvm", {}).get("args", []),
        "native": {
            "builder": build.get("builder", "host"),
            "optimization": str(native.get("optimization", "3")),
            "gc": gc,
            "monitoring": sorted(monitoring),
            # Reported only when set, so historical options are unchanged.
            **({"maxHeap": native["maxHeap"]} if "maxHeap" in native else {}),
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


@dataclass(frozen=True)
class ProfileContract:
    """What prepare and evidence require of a run's recipes.

    Chosen from the manifest identity: a historical manifest keeps the
    historical contract, and experiment 01 is recognised by its own block.
    """

    shared: str
    functions: Mapping[str, tuple[str, str]]

    def modules(self, key: str) -> frozenset[str]:
        """Return the exact module set the profile `key` must be built with."""
        if self.shared == "jvm":
            return COMPARISON_RECIPE_MODULES
        return frozenset(EXPERIMENT_01_VARIANTS_BY_KEY[key].modules)

    def options(self, key: str, profile: Path) -> dict[str, object]:
        """Return the declared options recorded for the profile `key`."""
        if self.shared == "jvm":
            return declared_options(profile)
        return experiment_01_options(
            profile, EXPERIMENT_01_VARIANTS_BY_KEY[key], shares=key == self.shared
        )


HISTORICAL_CONTRACT = ProfileContract("jvm", COMPARISON_FUNCTIONS)
EXPERIMENT_01_CONTRACT = ProfileContract(
    next(iter(EXPERIMENT_01_VARIANTS_BY_KEY)), EXPERIMENT_01_FUNCTIONS
)


def contract_for(identity: Mapping[str, object]) -> ProfileContract:
    """Return the recipe contract a manifest identity was captured under."""
    return (
        EXPERIMENT_01_CONTRACT
        if identity.get("experiment") is not None
        else HISTORICAL_CONTRACT
    )
