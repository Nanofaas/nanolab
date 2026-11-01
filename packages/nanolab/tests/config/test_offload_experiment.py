"""B4 rejects configurations it cannot measure faithfully."""

import pytest

from nanolab.config.scenario import ScenarioConfig


def scenario():
    return {
        "workflow": "offload-loadtest",
        "backend": "k8s",
        "functions": ["word-stats-java"],
        "offloadExperiment": {"mode": "pressure", "rate": 100},
        "resources": {
            "control-plane": {
                "requests": {"cpu": 0.25, "memoryMiB": 512},
                "limits": {"cpu": 2, "memoryMiB": 2048},
            },
            "word-stats-java": {
                "requests": {"cpu": 1, "memoryMiB": 256},
                "limits": {"cpu": 2, "memoryMiB": 512},
            },
        },
    }


def test_explicit_matched_arm():
    config = ScenarioConfig.model_validate(scenario())
    assert config.offload_experiment.mode == "pressure"
    assert config.offload_experiment.rate == 100


@pytest.mark.parametrize(
    "change",
    [
        {"autoscaling": True},
        {"loadScale": 2},
        {"backend": "container"},
        {"resources": {}},
        {"functions": ["word-stats-java", "json-transform-java"]},
        {"offloadExperiment": {"mode": "pressure", "rate": 0}},
        {"workflow": "loadtest"},
        {"payloadProfile": "small"},
    ],
)
def test_unmeasured_options_are_rejected(change):
    with pytest.raises(ValueError, match=r"requires|does not consume|greater than"):
        ScenarioConfig.model_validate(scenario() | change)
