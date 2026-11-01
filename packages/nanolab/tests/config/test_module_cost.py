import pytest

from nanolab.config.scenario import ScenarioConfig


def config(**changes):
    return {
        "workflow": "module-cost",
        "backend": "k8s",
        "functions": ["word-stats-java"],
        "moduleCost": {"blockId": "block-1"},
        **changes,
    }


def test_protocol_accepts_only_its_measured_options():
    parsed = ScenarioConfig.model_validate(config())
    assert parsed.module_cost.block_id == "block-1"
    with pytest.raises(
        ValueError, match=r"module-cost|moduleCost|pattern|Extra|absolute"
    ):
        ScenarioConfig.model_validate(config(loadScale=2))


@pytest.mark.parametrize(
    "change",
    [
        {"functions": ["word-stats-rust"]},
        {"backend": "container"},
        {"moduleCost": {"blockId": "../escape"}},
        {"moduleCost": {"blockId": "x", "order": ["jvm-base"] * 8}},
        {"moduleCost": {"blockId": "x", "heapMiB": 32}},
    ],
)
def test_protocol_rejects_unmatched_configuration(change):
    with pytest.raises(
        ValueError, match=r"module-cost|moduleCost|pattern|Extra|absolute"
    ):
        ScenarioConfig.model_validate(config(**change))
