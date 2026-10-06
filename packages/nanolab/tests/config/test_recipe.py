from __future__ import annotations

from pathlib import Path

import pytest

from nanolab.cli.product import _scenario
from nanolab.config.scenario import ScenarioConfig
from nanolab.workspace.paths import discover_tool_root


def test_scenario_resolves_recipe_relative_to_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles = tmp_path / "recipes"
    profiles.mkdir()
    recipe = profiles / "profile.yaml"
    recipe.write_text("schemaVersion: 2\n")
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    scenario = scenarios / "validate.yaml"
    scenario.write_text(
        "workflow: validate\nbackend: container\n"
        "functions: [word-stats-java]\nrecipeProfile: ../recipes/profile.yaml\n"
    )
    monkeypatch.chdir(tmp_path.parent)
    config = _scenario(scenario)
    assert config.recipe_profile == recipe.resolve()
    assert config.model_copy().recipe_profile == recipe.resolve()


@pytest.mark.parametrize(
    "settings",
    [
        {"build": "buildpack"},
        {"controlPlaneImage": "custom"},
        {"asyncLoad": True},
    ],
)
def test_recipe_rejects_unsupported_scenario_options(
    tmp_path: Path, settings: dict[str, object]
) -> None:
    data: dict[str, object] = {
        "workflow": "validate",
        "backend": "container",
        "functions": ["word-stats-java"],
        "recipeProfile": str(tmp_path / "profile.yaml"),
    }
    data.update(settings)
    with pytest.raises(ValueError, match="recipeProfile"):
        ScenarioConfig.model_validate(data)


def test_kubernetes_recipe_profile_is_accepted(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("schemaVersion: 2\n")

    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": "k8s",
            "build": "docker",
            "functions": ["word-stats-java"],
            "recipeProfile": str(profile),
        }
    )

    assert config.recipe_profile == profile


def test_container_loadtest_accepts_recipe_profile(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("schemaVersion: 2\n")

    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "container",
            "functions": ["word-stats-java"],
            "autoscaling": True,
            "recipeProfile": str(profile),
        }
    )

    assert config.recipe_profile == profile


@pytest.mark.parametrize(
    "settings",
    [
        {"backend": "containerd"},
        {"backend": "k8s"},
        {"controlPlaneImage": "custom"},
        {"controlPlaneVariant": "jvm"},
        {"functionImages": {"word-stats-java": "custom"}},
        {"controlPlaneRuntime": "native"},
        {"build": "buildpack"},
    ],
)
def test_container_loadtest_recipe_rejects_incompatible_options(
    tmp_path: Path, settings: dict[str, object]
) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("schemaVersion: 2\n")
    data: dict[str, object] = {
        "workflow": "loadtest",
        "backend": "container",
        "functions": ["word-stats-java"],
        "recipeProfile": str(profile),
    }
    data.update(settings)

    with pytest.raises(ValueError, match="recipeProfile"):
        ScenarioConfig.model_validate(data)


def test_autoscaling_container_scenario_selects_loadtest_recipe() -> None:
    scenario = discover_tool_root() / "scenarios/autoscaling-cycle-container.yaml"

    config = _scenario(scenario)

    assert (
        config.recipe_profile
        == (scenario.parent.parent / "recipes/loadtest-container-jvm.yaml").resolve()
    )
