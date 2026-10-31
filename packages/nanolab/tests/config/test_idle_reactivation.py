import pytest
from pydantic import ValidationError

from nanolab.config.scenario import ScenarioConfig


def idle_scenario(runtime="java", **changes):
    function = f"word-stats-{runtime}"
    value = {
        "workflow": "idle-reactivation",
        "backend": "k8s",
        "functions": [function],
        "idleReactivation": {},
        "resources": {
            "control-plane": {
                "requests": {"cpu": 0.25, "memoryMiB": 512},
                "limits": {"cpu": 2, "memoryMiB": 2048},
            },
            function: {
                "requests": {"cpu": 1, "memoryMiB": 256},
                "limits": {"cpu": 2, "memoryMiB": 512},
            },
        },
    }
    return value | changes


@pytest.mark.parametrize("runtime", ["java", "go", "javascript", "python", "rust"])
def test_dedicated_workflow_has_a_bounded_paired_pilot(runtime):
    scenario = ScenarioConfig.model_validate(idle_scenario(runtime))
    assert scenario.idle_reactivation is not None
    assert scenario.idle_reactivation.cycles == 3
    assert scenario.idle_reactivation.idle_seconds == 180
    assert scenario.idle_reactivation.zero_first is False


@pytest.mark.parametrize(
    "change",
    [
        {"idleReactivation": None},
        {"backend": "container"},
        {"loadProfile": "cycle"},
        {"autoscaling": True},
        {"loadVus": 2},
        {"controlPlaneImage": "x:y"},
        {"recipeProfile": "anything.yaml"},
        {"functions": ["word-stats-java-lite"]},
        {"resources": {}},
        {"idleReactivation": {"cycles": 0}},
        {"idleReactivation": {"idleSeconds": 59}},
    ],
)
def test_dedicated_workflow_rejects_unused_or_unmatched_settings(change):
    with pytest.raises(ValidationError):
        ScenarioConfig.model_validate(idle_scenario(**change))


def test_idle_protocol_cannot_be_ignored_by_another_workflow():
    with pytest.raises(ValidationError, match="idleReactivation"):
        ScenarioConfig.model_validate(
            {
                "workflow": "loadtest",
                "functions": ["word-stats-java"],
                "idleReactivation": {},
            }
        )
