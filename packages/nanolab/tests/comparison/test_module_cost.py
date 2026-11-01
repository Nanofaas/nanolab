from pathlib import Path

import pytest

from nanolab.config.module_cost import ARMS, BASE


def test_matrix_preserves_base_and_fixed_build_parameters():
    from nanolab.comparison.module_cost import module_cost_cells

    cells = module_cost_cells("block-1")
    assert tuple(cells) == ARMS
    for key, recipe in cells.items():
        cp = recipe["controlPlane"]
        assert set(BASE) <= set(cp["modules"])
        assert cp["container"]["image"] == key
        if key.startswith("jvm"):
            assert "-Xmx64m" in cp["jvm"]["args"]
        else:
            assert cp["build"]["native"] == {
                "optimization": "3",
                "gc": "serial",
                "maxHeap": "64m",
            }
    assert len({c["controlPlane"]["container"]["image"] for c in cells.values()}) == 8
    assert cells["jvm-cs"]["controlPlane"]["modules"] == [
        *BASE,
        "concurrency-control",
        "autoscaler",
    ]


def test_preparation_is_unique_validated_and_has_no_cloud_effects(tmp_path):
    import json

    import yaml

    from nanolab.comparison.module_cost import prepare_campaign
    from nanolab.config import EnvironmentConfig, ScenarioConfig

    root = prepare_campaign(tmp_path, Path("/controller/key.pub"), 23)
    manifest = json.loads((root / "manifest.json").read_text())
    assert len(manifest["blocks"]) == 3
    for block in manifest["blocks"]:
        directory = root / block
        scenario = ScenarioConfig.model_validate(
            yaml.safe_load((directory / "scenario.yaml").read_text())
        )
        env = EnvironmentConfig.model_validate(
            yaml.safe_load((directory / "environment.yaml").read_text())
        )
        assert env.azure.priority == "Spot"
        assert env.roles["stack"].name != env.roles["loadgen"].name
        assert set(scenario.module_cost.order) == set(ARMS)
    assert prepare_campaign(tmp_path, Path("/controller/key.pub"), 23) != root
    with pytest.raises(ValueError, match="module-cost|moduleCost|pattern|Extra|absolute") :
        prepare_campaign(tmp_path, Path("relative.pub"), 23)
