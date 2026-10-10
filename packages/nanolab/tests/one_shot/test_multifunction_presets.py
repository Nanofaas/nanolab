"""Packaged templates keep strict resources and six-minute scope explicit."""

from importlib.resources import files

import pytest
import yaml

from nanolab.config.scenario import ScenarioConfig


@pytest.mark.parametrize("stage", ["calibration", "qualification", "pilot"])
def test_multifunction_templates_are_strict_and_block_unfilled_artifacts(stage):
    path = files("nanolab").joinpath(
        "assets/presets/scenarios/one-shot-multifunction-" + stage + ".yaml"
    )
    config = ScenarioConfig.model_validate(yaml.safe_load(path.read_text()))
    settings = config.one_shot
    assert settings is not None and settings.strict_memory
    assert len(config.functions) == len(settings.functions) == 5
    assert [node.cpus for node in settings.nodes] == [1, 1, 2, 2, 3, 8]
    assert [node.memory_capacity_mib for node in settings.nodes] == [
        320,
        320,
        640,
        640,
        960,
        2240,
    ]
    assert [
        next(iter(node.function_limits.values())).max_replicas
        for node in settings.nodes
    ] == [5, 5, 10, 10, 15, 35]
    assert {fn.memory_mib for fn in settings.functions.values()} == {64}
    assert settings.observability.logs.enabled
    assert (
        settings.runtime_distribution is not None
        and settings.runtime_distribution.sha256 == "0" * 64
    )
    if stage == "qualification":
        assert settings.timing is not None
        assert settings.timing.period_candidates == [360]
        assert settings.timing.minimum_samples == 50
        assert settings.timing.quantile == 0.95 and settings.timing.epsilon == 0.05
        assert settings.timing.protocol.preparation_seconds == 1
    elif stage == "pilot":
        assert settings.experiment is not None
        assert settings.experiment.warmup_seconds == 720
        assert all(window.epochs == 1 for window in settings.experiment.windows)
