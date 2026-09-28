from __future__ import annotations

from pathlib import Path

import pytest

from nanolab.cli.product import _scenario
from nanolab.config.scenario import ScenarioConfig


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
        {"backend": "k8s"},
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
