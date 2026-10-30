"""Reusable recipe profiles and the prepared comparison handoff."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml

from nanolab.comparison.experiment_02 import (
    CONFIGURATIONS_BY_KEY,
    EXPERIMENT_02_PUBLISHER,
    Configuration,
)
from nanolab.config.experiment_01 import CONTROL_PLANE_MAX_HEAP_MIB
from nanolab.images.control_plane_variants import (
    EXPERIMENT_01_VARIANTS_BY_KEY,
    VARIANTS_BY_KEY,
    ControlPlaneVariant,
    experiment_01_catalog,
    runtime_class,
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
# Experiment 02 drives the same function; its P1 JVM recipe publishes it.
EXPERIMENT_02_FUNCTIONS = {"qr-code-rust": ("qr-code", "rust")}
EXPERIMENT_02_MODULES: dict[Literal["P0", "P1"], tuple[str, ...]] = {
    # The core and nothing a deployment needs: an external endpoint, no queue.
    "P0": ("build-metadata",),
    "P1": ("container-deployment-provider", "async-queue", "build-metadata"),
}
_HEAP = f"-Xmx{CONTROL_PLANE_MAX_HEAP_MIB}m"
# A native image takes its heap limit at build time (-R:MaxHeapSize).
_NATIVE_HEAP = f"{CONTROL_PLANE_MAX_HEAP_MIB}m"
_JVM_TIER_ARGS = {
    "interpreted": ["-Xint"],
    "c1": ["-XX:TieredStopAtLevel=1"],
    "tiered": [],
}


def runtime_gc_options(profile: Path, key: str, *, shares: bool) -> dict[str, object]:
    """Verify the campaign constraints using the existing recipe options."""
    options = declared_options(profile)
    recipe = yaml.safe_load(profile.read_text())
    cp = recipe["controlPlane"]
    variant = VARIANTS_BY_KEY[key]
    if cp["build"].get("variant") != key or cp.get("config"):
        raise ValueError("runtime GC recipe identity/configuration differs")
    if key.startswith("native-"):
        expected = {
            "builder": "container",
            "distribution": "oracle" if key.endswith("g1") else "community",
            "gc": "G1" if key.endswith("g1") else "serial",
            "monitoring": ["jfr"] if key.endswith("g1") else [],
            "optimization": variant.build_env["NATIVE_OPTIMIZATION"],
            "maxHeap": _NATIVE_HEAP,
        }
        if options["mode"] != "native" or options["native"] != expected or "jvm" in cp:
            raise ValueError("runtime GC native options differ")
    else:
        expected_args = ["-XX:+UseG1GC" if "g1" in key else "-XX:+UseSerialGC", _HEAP]
        if runtime_class(variant) == "c1":
            expected_args.append("-XX:TieredStopAtLevel=1")
        if options["mode"] != "jvm" or options["jvm_args"] != expected_args:
            raise ValueError("runtime GC JVM options differ")
    expected_functions = [("qr-code", "rust")] if shares else []
    if [
        (f["name"], f["sdk"]) for f in recipe.get("functions", [])
    ] != expected_functions or recipe.get("services"):
        raise ValueError("only the runtime GC publisher declares QR")
    return options


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
    catalog = experiment_01_catalog(variants)
    shared = next(iter(catalog))
    runtime_gc = shared == "jvm"
    ordered = (shared, *(key for key in variants if key != shared))
    profiles = {
        key: tool_root
        / "recipes"
        / f"{'experiment-01-runtime-' if runtime_gc else ''}{key}.yaml"
        for key in ordered
    }
    for key, profile in profiles.items():
        if runtime_gc:
            runtime_gc_options(profile, key, shares=key == shared)
        else:
            experiment_01_options(profile, catalog[key], shares=key == shared)
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
    _require_modules(cp, variant.modules, variant.key)
    runtime = variant.runtime
    if runtime is None:
        raise ValueError(f"{variant.key} declares no runtime class")
    _require_build(cp, runtime, variant.key)
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


def _require_modules(cp: Mapping[str, Any], modules: tuple[str, ...], key: str) -> None:
    """Require exactly `modules`, no more, no fewer and no repeats."""
    selected = cp["modules"]
    if sorted(selected) != sorted(modules) or len(selected) != len(modules):
        raise ValueError(f"{key} requires modules {list(modules)}")


def _require_build(cp: Mapping[str, Any], runtime: str, key: str) -> None:
    """Require the serial collector, the 64 MiB heap and the runtime's tier.

    Shared by experiments 01 and 02, which hold every runtime to the same heap
    and collector so that only the composition differs between their builds.
    """
    build = cp["build"]
    if "variant" in build or cp.get("config"):
        raise ValueError(
            "experiment recipes carry no variant label or extra configuration"
        )
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
                f"{key} must be a serial native -O build, maxHeap {_NATIVE_HEAP}"
            )
    elif build["mode"] != "jvm" or cp.get("jvm", {}).get("args") != [
        "-XX:+UseSerialGC",
        _HEAP,
        *_JVM_TIER_ARGS[runtime],
    ]:
        raise ValueError(f"{key} must run serial GC, {_HEAP} and its tier")


# Experiment 02 compares complete platforms, so each nanoFaaS composition runs
# at its best tier: full JIT on the JVM, -O3 in native mode.
_EXPERIMENT_02_RUNTIME = {"jvm": "tiered", "native": "native-o3"}


def experiment_02_profiles(tool_root: Path, keys: tuple[str, ...]) -> dict[str, Path]:
    """Resolve the recipes the selected experiment-02 configurations need.

    The function publisher comes first even when it is not selected: every
    configuration, the function alone included, runs the image it publishes.
    Configurations without a recipe contribute nothing.
    """
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("experiment-02 configurations must be nonempty and unique")
    unknown = set(keys) - CONFIGURATIONS_BY_KEY.keys()
    if unknown:
        raise ValueError(
            "unknown experiment-02 configurations: " + ", ".join(sorted(unknown))
        )
    ordered = dict.fromkeys((EXPERIMENT_02_PUBLISHER, *keys))
    profiles: dict[str, Path] = {}
    for key in ordered:
        configuration = CONFIGURATIONS_BY_KEY[key]
        if configuration.recipe is None:
            continue
        profiles[key] = tool_root / "recipes" / f"{configuration.recipe}.yaml"
        _ = experiment_02_options(
            profiles[key], configuration, shares=key == EXPERIMENT_02_PUBLISHER
        )
    return profiles


def experiment_02_options(
    profile: Path, configuration: Configuration, *, shares: bool
) -> dict[str, object]:
    """Read a recipe and require exactly the build the configuration declares."""
    if configuration.profile is None or configuration.runtime is None:
        raise ValueError(f"{configuration.key} is not built from a recipe")
    recipe = yaml.safe_load(profile.read_text())
    cp = recipe["controlPlane"]
    modules = EXPERIMENT_02_MODULES[configuration.profile]
    _require_modules(cp, modules, configuration.key)
    runtime = _EXPERIMENT_02_RUNTIME[configuration.runtime]
    _require_build(cp, runtime, configuration.key)
    functions = [
        (function["name"], function["sdk"]) for function in recipe.get("functions", [])
    ]
    expected = list(EXPERIMENT_02_FUNCTIONS.values()) if shares else []
    if functions != expected or recipe.get("services"):
        raise ValueError(
            f"only {EXPERIMENT_02_PUBLISHER} publishes the shared function"
        )
    return {
        **declared_options(profile, modules=frozenset(modules)),
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
            **(
                {"distribution": "oracle" if gc == "G1" else "community"}
                if build.get("builder") == "container"
                else {}
            ),
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
    historical contract, and each experiment is recognised by its own block.
    """

    kind: Literal["historical", "experiment-01", "experiment-02", "runtime-gc"]
    shared: str
    functions: Mapping[str, tuple[str, str]]

    def modules(self, key: str) -> frozenset[str]:
        """Return the exact module set the profile `key` must be built with."""
        if self.kind in {"historical", "runtime-gc"}:
            return COMPARISON_RECIPE_MODULES
        if self.kind == "experiment-02":
            profile = CONFIGURATIONS_BY_KEY[key].profile
            if profile is None:
                raise ValueError(f"{key} is not built from a recipe")
            return frozenset(EXPERIMENT_02_MODULES[profile])
        return frozenset(EXPERIMENT_01_VARIANTS_BY_KEY[key].modules)

    def options(self, key: str, profile: Path) -> dict[str, object]:
        """Return the declared options recorded for the profile `key`."""
        if self.kind == "runtime-gc":
            return runtime_gc_options(profile, key, shares=key == self.shared)
        if self.kind == "historical":
            return declared_options(profile)
        if self.kind == "experiment-02":
            return experiment_02_options(
                profile, CONFIGURATIONS_BY_KEY[key], shares=key == self.shared
            )
        return experiment_01_options(
            profile, EXPERIMENT_01_VARIANTS_BY_KEY[key], shares=key == self.shared
        )


HISTORICAL_CONTRACT = ProfileContract("historical", "jvm", COMPARISON_FUNCTIONS)
EXPERIMENT_01_CONTRACT = ProfileContract(
    "experiment-01", next(iter(EXPERIMENT_01_VARIANTS_BY_KEY)), EXPERIMENT_01_FUNCTIONS
)
EXPERIMENT_02_CONTRACT = ProfileContract(
    "experiment-02", EXPERIMENT_02_PUBLISHER, EXPERIMENT_02_FUNCTIONS
)


def contract_for(identity: Mapping[str, object]) -> ProfileContract:
    """Return the recipe contract a manifest identity was captured under."""
    experiment = identity.get("experiment")
    if experiment is None:
        return HISTORICAL_CONTRACT
    if isinstance(experiment, Mapping) and experiment.get("profile") == "experiment-02":
        return EXPERIMENT_02_CONTRACT
    if isinstance(experiment, Mapping) and experiment.get("builds"):
        catalog = experiment_01_catalog(tuple(experiment.get("builds", {})))
        if next(iter(catalog)) == "jvm":
            return ProfileContract("runtime-gc", "jvm", EXPERIMENT_01_FUNCTIONS)
    return EXPERIMENT_01_CONTRACT
