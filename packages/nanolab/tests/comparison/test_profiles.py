"""Comparison profiles preserve the selected runtime and shared functions."""

from pathlib import Path

import pytest
import yaml

from nanolab.comparison.profiles import comparison_profiles, declared_options

ROOT = Path(__file__).resolve().parents[2]
JVM = {
    "jvm": ["-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1"],
    "jvm-g1": ["-XX:+UseG1GC", "-XX:TieredStopAtLevel=1"],
    "jvm-c2": ["-XX:+UseSerialGC"],
    "jvm-g1-c2": ["-XX:+UseG1GC"],
    "jvm-loop1": [
        "-XX:+UseSerialGC",
        "-XX:TieredStopAtLevel=1",
        "-Dreactor.netty.ioWorkerCount=1",
    ],
    "jvm-c2-loop1": ["-XX:+UseSerialGC", "-Dreactor.netty.ioWorkerCount=1"],
}
NATIVE = {
    "native-os": ("s", "serial", []),
    "native-o3": ("3", "serial", []),
    "native-o3-g1": ("3", "G1", ["jfr"]),
}


def test_nonbaseline_selection_prepares_shared_functions_first():
    selected = comparison_profiles(ROOT, ("native-o3", "jvm-g1"))
    assert list(selected) == ["jvm", "native-o3", "jvm-g1"]
    assert selected["jvm"].name == "comparison-jvm.yaml"


@pytest.mark.parametrize("selection", [(), ("unknown",), ("jvm", "jvm")])
def test_invalid_selection_refuses_preparation(selection):
    with pytest.raises(ValueError, match="comparison"):
        comparison_profiles(ROOT, selection)


@pytest.mark.parametrize("key", [*JVM, *NATIVE])
def test_profile_declares_runtime_and_only_baseline_functions(key):
    path = comparison_profiles(ROOT, (key,))[key]
    recipe = yaml.safe_load(path.read_text())
    options = declared_options(path)
    assert recipe["schemaVersion"] == 2
    assert recipe["name"] == f"comparison-{key}"
    assert recipe["registry"]["repository"] == "127.0.0.1:5000/nanofaas"
    assert recipe["controlPlane"]["container"]["image"] == f"control-plane-{key}"
    assert options["variant"] == key
    assert isinstance(options["modules"], list)
    assert set(options["modules"]) == {
        "k8s-deployment-provider",
        "async-queue",
        "build-metadata",
    }
    if key in JVM:
        assert options["mode"] == "jvm"
        assert options["jvm_args"] == JVM[key]
    else:
        optimization, gc, monitoring = NATIVE[key]
        assert options["mode"] == "native"
        assert options["native"] == {
            "builder": "host",
            "optimization": optimization,
            "gc": gc,
            "monitoring": monitoring,
        }
    assert options["functions"] == (
        [
            {
                "name": "word-stats",
                "sdk": "java",
                "mode": "jvm",
                "image": "java-word-stats",
            },
            {
                "name": "word-stats",
                "sdk": "javascript",
                "mode": None,
                "image": "javascript-word-stats",
            },
        ]
        if key == "jvm"
        else []
    )


@pytest.mark.parametrize("change", ["sync", "strategy", "admin", "admin-canonical"])
def test_conflicting_scheduler_profile_is_rejected(tmp_path, change):
    recipe = yaml.safe_load(comparison_profiles(ROOT, ("jvm",))["jvm"].read_text())
    if change == "sync":
        recipe["controlPlane"]["modules"].append("sync-queue")
    elif change == "strategy":
        recipe["controlPlane"]["config"] = {
            "nanofaas": {"scheduler": {"strategy": "shared-queue"}}
        }
    elif change == "admin-canonical":
        recipe["controlPlane"]["config"] = {
            "nanofaas": {"admin": {"runtime-config": {"enabled": True}}}
        }
    else:
        recipe["controlPlane"]["config"] = {
            "nanofaas": {"runtime-config": {"admin": {"enabled": True}}}
        }
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(recipe))
    with pytest.raises(ValueError, match="comparison"):
        declared_options(path)


@pytest.mark.parametrize(
    "config",
    [
        {"nanofaas.scheduler.strategy": "shared-queue"},
        {"nanofaas": {"scheduler.strategy": "shared-queue"}},
        {"nanofaas.admin.runtime-config.enabled": True},
        {"nanofaas": {"admin": {"runtimeConfig": {"enabled": "true"}}}},
    ],
)
def test_flat_or_relaxed_spring_configuration_cannot_override_scheduler(
    tmp_path, config
):
    recipe = yaml.safe_load(comparison_profiles(ROOT, ("jvm",))["jvm"].read_text())
    recipe["controlPlane"]["config"] = config
    profile = tmp_path / "changed.yaml"
    profile.write_text(yaml.safe_dump(recipe))
    with pytest.raises(ValueError, match="scheduler"):
        declared_options(profile)
