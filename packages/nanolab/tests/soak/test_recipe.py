"""Recipe selection and snapshot staging at the public soak boundary."""

import copy
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from nanolab.cli.product import _scenario
from nanolab.config.scenario import ScenarioConfig
from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.sources import capture_source_snapshot
from nanolab.workspace.paths import discover_tool_root


def smoke_data():
    path = discover_tool_root() / "scenarios/memory-soak-smoke-container.yaml"
    data = yaml.safe_load(path.read_text())
    for image in data["soak"]["images"].values():
        image["platform"] = "linux/arm64"
    return data


def profile_data():
    return {
        "schemaVersion": 2,
        "name": "soak-container-smoke-jvm",
        "registry": {
            "repository": "127.0.0.1:5000/nanofaas",
            "tag": "smoke",
            "platforms": ["linux/arm64"],
            "provenance": True,
        },
        "controlPlane": {
            "modules": [
                "container-deployment-provider",
                "async-queue",
                "build-metadata",
            ],
            "build": {"mode": "jvm", "variant": "jvm"},
            "container": {"image": "control-plane"},
        },
        "functions": [
            {
                "name": "word-stats",
                "sdk": "java",
                "build": {"mode": "jvm"},
                "container": {"image": "java-word-stats"},
            },
            {
                "name": "word-stats",
                "sdk": "javascript",
                "container": {"image": "javascript-word-stats"},
            },
        ],
    }


def write_profile(path, data=None):
    path.write_text(yaml.safe_dump(profile_data() if data is None else data))
    return path


def test_container_smoke_accepts_recipe_profile(tmp_path):
    profile = write_profile(tmp_path / "recipe.yaml")
    data = smoke_data()
    data["recipeProfile"] = str(profile)
    config = ScenarioConfig.model_validate(data)
    assert config.recipe_profile == profile


def test_soak_recipe_path_is_relative_to_scenario(tmp_path, monkeypatch):
    profile = write_profile(tmp_path / "recipe.yaml")
    directory = tmp_path / "scenarios"
    directory.mkdir()
    data = smoke_data()
    data["recipeProfile"] = "../recipe.yaml"
    scenario = directory / "soak.yaml"
    scenario.write_text(yaml.safe_dump(data))
    monkeypatch.chdir(tmp_path.parent)
    assert _scenario(scenario).recipe_profile == profile


def test_checked_in_recipe_smoke_resolves_and_matches_policy():
    from nanolab.tasks.soak.recipe import validate_soak_recipe

    path = discover_tool_root() / "scenarios/memory-soak-smoke-recipe-container.yaml"
    config = _scenario(path)
    assert config.recipe_profile is not None
    assert config.soak is not None
    validate_soak_recipe(config.recipe_profile, config.soak, platform="linux/arm64")
    assert config.soak.purpose == "smoke"
    assert config.soak.phases.steady_s == 60


@pytest.mark.parametrize(
    "change",
    [
        {"backend": "containerd"},
        {"backend": "k8s"},
        {"controlPlaneImage": "override"},
        {"functionImages": {"word-stats-java": "override"}},
        {"build": "buildpack"},
    ],
)
def test_soak_recipe_rejects_unsupported_selection(tmp_path, change):
    data = smoke_data()
    data["recipeProfile"] = str(write_profile(tmp_path / "recipe.yaml"))
    data.update(change)
    with pytest.raises(ValueError, match="recipeProfile"):
        ScenarioConfig.model_validate(data)


def test_soak_recipe_matches_all_role_expectations(tmp_path):
    from nanolab.tasks.soak.recipe import validate_soak_recipe

    profile = write_profile(tmp_path / "recipe.yaml")
    validate_soak_recipe(
        profile, SoakConfig.model_validate(smoke_data()["soak"]), platform="linux/arm64"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "provenance",
        "platform",
        "modules",
        "extra-function",
        "function-mode",
        "repository",
        "schema",
        "prebuilt",
        "variant",
        "override",
        "services",
    ],
)
@pytest.mark.parametrize("purpose", ["smoke", "p24"])
def test_soak_recipe_rejects_incompatible_profile(tmp_path, mutation, purpose):
    from nanolab.tasks.soak.recipe import validate_soak_recipe

    profile = profile_data() if purpose == "smoke" else p24_profile_data()
    config = (
        smoke_data()["soak"]
        if purpose == "smoke"
        else p24_config().model_dump(mode="json")
    )
    if mutation == "provenance":
        profile["registry"]["provenance"] = False
    elif mutation == "platform":
        profile["registry"]["platforms"] = ["linux/amd64"]
    elif mutation == "modules":
        profile["controlPlane"]["modules"].remove("async-queue")
    elif mutation == "extra-function":
        profile["functions"].append(copy.deepcopy(profile["functions"][0]))
    elif mutation == "function-mode":
        profile["functions"][0]["build"]["mode"] = "native"
    elif mutation == "repository":
        profile["registry"]["repository"] = "example.com/images"
    elif mutation == "schema":
        profile["schemaVersion"] = 1
    elif mutation == "prebuilt":
        config["images"]["control-plane"].update(
            mode="prebuilt",
            digest="example/cp@sha256:" + "a" * 64,
            provenance_receipt="receipt.json",
        )
    elif mutation == "variant":
        config["images"]["control-plane"]["variant"] = "jvm-other"
    elif mutation == "override":
        config["images"]["control-plane"]["build_options"] = {"RUNTIME_IMAGE": "other"}
    elif mutation == "services":
        profile["services"] = [{"name": "warm-echo", "sdk": "java"}]
    parsed = SoakConfig.model_validate(config)
    with pytest.raises(ValueError, match="recipe"):
        validate_soak_recipe(
            write_profile(tmp_path / "recipe.yaml", profile),
            parsed,
            platform="linux/arm64",
        )


def snapshot_for(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "tracked.txt").write_text("original\n")
    (source / "deleted.txt").write_text("remove\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=source,
        check=True,
    )
    (source / "tracked.txt").write_text("dirty\n")
    (source / "deleted.txt").unlink()
    (source / "untracked.sh").write_text("#!/bin/sh\necho untracked\n")
    (source / "untracked.sh").chmod(0o755)
    (source / "internal-link").symlink_to("tracked.txt")
    return capture_source_snapshot(source, tmp_path / "snapshot", max_bytes=1024 * 1024)


def test_recipe_staging_preserves_dirty_and_untracked_snapshot(tmp_path):
    from nanolab.tasks.soak.recipe import prepare_soak_recipe_run

    snapshot = snapshot_for(tmp_path)
    profile = write_profile(tmp_path / "profile.yaml")
    run = prepare_soak_recipe_run(snapshot, profile, tmp_path / "run", "unique-run")
    assert (run.source_dir / "tracked.txt").read_text() == "dirty\n"
    assert (
        run.source_dir / "untracked.sh"
    ).read_text() == "#!/bin/sh\necho untracked\n"
    assert (run.source_dir / "untracked.sh").stat().st_mode & 0o111
    assert (run.source_dir / "internal-link").readlink() == Path("tracked.txt")
    assert not (run.source_dir / "deleted.txt").exists()
    assert run.recipe.read_bytes() == profile.read_bytes()
    identity = json.loads((tmp_path / "run/source-identity.json").read_text())
    assert identity["revision"] == snapshot.revision
    assert identity["dirty"] is True
    assert identity["fingerprint"] == snapshot.fingerprint
    assert identity["staging_revision"] != snapshot.revision


def test_recipe_staging_rejects_mutation(tmp_path):
    from nanolab.tasks.soak.recipe import prepare_soak_recipe_run

    snapshot = snapshot_for(tmp_path)
    (snapshot.root / "tracked.txt").write_text("changed snapshot\n")
    with pytest.raises(ValueError, match=r"snapshot.*frozen identity"):
        prepare_soak_recipe_run(
            snapshot,
            write_profile(tmp_path / "profile.yaml"),
            tmp_path / "run",
            "unique-run",
        )


def test_staging_git_identity_includes_captured_tracked_ignored_file(tmp_path):
    from nanolab.tasks.soak.recipe import prepare_soak_recipe_run

    snapshot = snapshot_for(tmp_path)
    source = tmp_path / "original"
    (source / ".gitignore").write_text("tracked.txt\n")
    snapshot = capture_source_snapshot(
        source, tmp_path / "second-snapshot", max_bytes=1024 * 1024
    )
    run = prepare_soak_recipe_run(
        snapshot,
        write_profile(tmp_path / "profile.yaml"),
        tmp_path / "run",
        "unique-run",
    )
    staged = subprocess.run(
        ["git", "show", "HEAD:tracked.txt"],
        cwd=run.source_dir,
        capture_output=True,
        text=True,
    )
    assert staged.returncode == 0
    assert staged.stdout == "dirty\n"


P24_BASELINE = Path(__file__).parents[1] / "fixtures/soak/p24-sync-config.json"
P24_SCENARIO = discover_tool_root() / "scenarios/memory-soak-sync-container.yaml"


def p24_config():
    return SoakConfig.model_validate(json.loads(P24_BASELINE.read_text())["soak"])


def test_p24_recipe_preset_preserves_experiment():
    baseline = json.loads(P24_BASELINE.read_text())
    baseline.pop("recipe_profile")
    config = _scenario(P24_SCENARIO)
    actual = config.model_dump(mode="json")
    actual.pop("recipe_profile")
    assert actual.pop("contract") is None
    assert actual == baseline
    assert (
        config.recipe_profile
        == P24_SCENARIO.parent.parent / "recipes/soak-container-p24-jvm.yaml"
    )
    assert config.soak is not None
    assert config.soak.purpose == "p24" and config.soak.metrics_profile == "advanced"
    assert config.soak.phases.model_dump() == {
        "warmup_s": 120,
        "baseline_drain_s": 2100,
        "baseline_window_s": 120,
        "steady_s": 5400,
        "drain_s": 2100,
        "cleanup_margin_s": 300,
    }
    assert config.soak.workload.rates == {
        "word-stats-java": 20,
        "word-stats-javascript": 20,
    }
    assert config.soak.workload.preallocated_vus == config.soak.workload.max_vus == 200


def test_p24_recipe_accepts_supported_profile(tmp_path):
    from nanolab.tasks.soak.recipe import validate_soak_recipe

    profile = write_profile(tmp_path / "recipe.yaml", p24_profile_data())
    validate_soak_recipe(profile, p24_config(), platform="linux/arm64")
    raw = yaml.safe_load(P24_SCENARIO.read_text())
    raw.pop("soakPolicyFile")
    raw["soak"] = p24_config().model_dump(mode="json")
    raw["recipeProfile"] = str(profile)
    assert ScenarioConfig.model_validate(raw).recipe_profile == profile


@pytest.mark.parametrize("role", ["controlPlane", "java", "javascript"])
@pytest.mark.parametrize("option", ["jvm", "config", "buildOverride"])
def test_p24_recipe_rejects_policy_drift_before_acquisition(tmp_path, role, option):
    from nanolab.tasks.soak.recipe import validate_soak_recipe

    data = p24_profile_data()
    target = (
        data["controlPlane"]
        if role == "controlPlane"
        else data["functions"][0 if role == "java" else 1]
    )
    if option == "jvm":
        target["jvm"] = {"args": ["-XX:+UseSerialGC"]}
    elif option == "config":
        target["config"] = {"nanofaas": {"changed": True}}
    else:
        target.setdefault("build", {})["native"] = {"optimization": "3"}
    with pytest.raises(ValueError, match=r"(?i)recipe"):
        validate_soak_recipe(
            write_profile(tmp_path / "recipe.yaml", data),
            p24_config(),
            platform="linux/arm64",
        )


def test_smoke_profile_keeps_explicit_jvm_options():
    from nanolab.tasks.soak.recipe import validate_soak_recipe

    config = _scenario(P24_SCENARIO.parent / "memory-soak-smoke-recipe-container.yaml")
    assert config.soak is not None and config.recipe_profile is not None
    validate_soak_recipe(config.recipe_profile, config.soak, platform="linux/arm64")


@pytest.mark.parametrize(
    "failure", [None, "missing-profile", "missing-policy", "bad-policy", "short-p24"]
)
def test_p24_recipe_loader_preserves_path_and_policy_guards(
    tmp_path, monkeypatch, failure
):
    root = tmp_path / "project"
    scenarios = root / "scenarios"
    scenarios.mkdir(parents=True)
    profile = write_profile(root / "recipe.yaml", p24_profile_data())
    raw = yaml.safe_load(P24_SCENARIO.read_text())
    raw["recipeProfile"] = "../recipe.yaml"
    raw["soakPolicyFile"] = "./policy.yaml"
    policy = scenarios / "policy.yaml"
    policy.write_bytes((P24_SCENARIO.parent / "memory-soak-policy.yaml").read_bytes())
    if failure == "missing-profile":
        profile.unlink()
    elif failure == "missing-policy":
        policy.unlink()
    elif failure == "bad-policy":
        policy.write_text("schema: unsupported\ncriteria: []\n")
    elif failure == "short-p24":
        raw["soak"]["phases"]["steady_s"] = 60
    scenario = scenarios / "p24.yaml"
    scenario.write_text(yaml.safe_dump(raw))
    monkeypatch.chdir(tmp_path.parent)
    if failure is None:
        assert _scenario(scenario).recipe_profile == profile
    else:
        with pytest.raises(ValueError, match=r"(?i)recipe|policy|steady"):
            _scenario(scenario)


def p24_profile_data():
    data = profile_data()
    data["controlPlane"]["jvm"] = {
        "args": ["-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1"]
    }
    return data


def test_p24_checked_in_recipe_preserves_effective_launcher_settings():
    config = _scenario(P24_SCENARIO)
    assert config.recipe_profile is not None
    data = yaml.safe_load(config.recipe_profile.read_text())
    assert data["controlPlane"]["jvm"] == {
        "args": ["-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1"]
    }
    assert all("jvm" not in function for function in data["functions"])


@pytest.mark.parametrize(
    "jvm",
    [
        None,
        {"args": []},
        {"args": ["-XX:+UseSerialGC"]},
        {"args": ["-XX:+UseG1GC", "-XX:TieredStopAtLevel=1"]},
        {"args": ["-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1"], "extra": True},
    ],
)
def test_p24_recipe_requires_existing_control_plane_tuning(tmp_path, jvm):
    from nanolab.tasks.soak.recipe import validate_soak_recipe

    data = p24_profile_data()
    if jvm is None:
        data["controlPlane"].pop("jvm")
    else:
        data["controlPlane"]["jvm"] = jvm
    with pytest.raises(ValueError, match=r"(?i)recipe"):
        validate_soak_recipe(
            write_profile(tmp_path / "recipe.yaml", data),
            p24_config(),
            platform="linux/arm64",
        )
