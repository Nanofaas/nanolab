"""Comparison profiles preserve the selected runtime and shared functions."""

import pytest
import yaml

from nanolab.comparison.profiles import comparison_profiles, declared_options
from nanolab.workspace.paths import discover_tool_root

JVM = {
    "jvm-int": ["-XX:+UseSerialGC", "-Xint"],
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
    selected = comparison_profiles(discover_tool_root(), ("native-o3", "jvm-g1"))
    assert list(selected) == ["jvm", "native-o3", "jvm-g1"]
    assert selected["jvm"].name == "comparison-jvm.yaml"


@pytest.mark.parametrize("selection", [(), ("unknown",), ("jvm", "jvm")])
def test_invalid_selection_refuses_preparation(selection):
    with pytest.raises(ValueError, match="comparison"):
        comparison_profiles(discover_tool_root(), selection)


@pytest.mark.parametrize("key", [*JVM, *NATIVE])
def test_profile_declares_runtime_and_only_baseline_functions(key):
    path = comparison_profiles(discover_tool_root(), (key,))[key]
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
            "builder": "container",
            "distribution": "oracle" if gc == "G1" else "community",
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
    recipe = yaml.safe_load(
        comparison_profiles(discover_tool_root(), ("jvm",))["jvm"].read_text()
    )
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
    recipe = yaml.safe_load(
        comparison_profiles(discover_tool_root(), ("jvm",))["jvm"].read_text()
    )
    recipe["controlPlane"]["config"] = config
    profile = tmp_path / "changed.yaml"
    profile.write_text(yaml.safe_dump(recipe))
    with pytest.raises(ValueError, match="scheduler"):
        declared_options(profile)


def test_august_settings_add_only_interpreter():
    from nanolab.cli.comparison import DEFAULT_VARIANTS
    from nanolab.comparison.matrix import build_matrix
    from nanolab.images.control_plane_variants import resolve_variants, runtime_class

    root = discover_tool_root()
    profiles = comparison_profiles(root, ("jvm-int",))
    assert tuple(profiles) == ("jvm", "jvm-int")
    baseline = yaml.safe_load(profiles["jvm"].read_text())
    interpreted = yaml.safe_load(profiles["jvm-int"].read_text())
    assert interpreted["controlPlane"]["modules"] == baseline["controlPlane"]["modules"]
    assert interpreted["controlPlane"]["jvm"]["args"] == ["-XX:+UseSerialGC", "-Xint"]
    assert "functions" not in interpreted
    assert declared_options(profiles["jvm-int"])["variant"] == "jvm-int"
    assert runtime_class(resolve_variants(("jvm-int",))[0]) == "interpreted"
    assert "jvm-int" not in DEFAULT_VARIANTS
    keys = (
        "jvm",
        "jvm-g1",
        "jvm-c2",
        "jvm-g1-c2",
        "native-os",
        "native-o3",
        "native-o3-g1",
        "jvm-int",
    )
    variants = resolve_variants(keys)
    assert len(build_matrix(variants, 3)) == 24
    for key, profile in comparison_profiles(root, keys).items():
        options = declared_options(profile)
        args = options["jvm_args"]
        assert isinstance(args, list)
        assert not any(arg.startswith(("-Xmx", "-Xms")) for arg in args)
        if key.startswith("native-"):
            native = options["native"]
            assert isinstance(native, dict)
            assert "maxHeap" not in native
            assert native["builder"] == "container"
    for name, cpu, memory in (
        ("cpu1", 1, 2048),
        ("cpu2", 2, 2048),
        ("mem1024", 2, 1024),
        ("mem512", 2, 512),
    ):
        scenario = yaml.safe_load(
            (root / "scenarios" / f"runtime-comparison-{name}.yaml").read_text()
        )
        assert scenario["loadProfile"] == "comparison"
        assert scenario["functions"] == ["word-stats-java", "word-stats-javascript"]
        assert scenario["resources"]["control-plane"]["limits"] == {
            "cpu": cpu,
            "memoryMiB": memory,
        }
