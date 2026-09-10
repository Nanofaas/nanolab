"""A scenario's per-function resources must survive concurrency control."""

from __future__ import annotations

from nanolab.config import ScenarioConfig
from nanolab.config.scenario import ResourceSpec
from nanolab.plans.loadtest import _concurrency_function_resources


def test_concurrency_uses_the_calibrated_default_when_the_scenario_says_nothing() -> (
    None
):
    config = ScenarioConfig(
        workflow="loadtest",
        backend="container",
        concurrencyControl=True,
        functions=["word-stats-java"],
    )

    assert (
        _concurrency_function_resources(config, "word-stats-java")["limits"]["cpu"]
        == 4.0
    )


def test_a_scenario_declared_cpu_cap_overrides_the_default() -> None:
    """The cap is a calibration, and calibration belongs to the scenario.

    It used to be overwritten unconditionally, so a scenario that declared
    resources for a function under concurrency control had them silently
    ignored — while the schema validates them as if they applied.
    """
    config = ScenarioConfig(
        workflow="loadtest",
        backend="container",
        concurrencyControl=True,
        functions=["word-stats-java"],
        resources={
            "word-stats-java": ResourceSpec.model_validate(
                {
                    "limits": {"cpu": 2, "memoryMiB": 512},
                    "requests": {"cpu": 1, "memoryMiB": 256},
                }
            )
        },
    )

    resolved = _concurrency_function_resources(config, "word-stats-java")

    assert resolved["limits"]["cpu"] == 2.0
    assert resolved["requests"]["cpu"] == 1.0


def test_an_unlisted_function_keeps_the_default() -> None:
    config = ScenarioConfig(
        workflow="loadtest",
        backend="container",
        concurrencyControl=True,
        functions=["word-stats-java", "word-stats-java-lite"],
        resources={
            "word-stats-java": ResourceSpec.model_validate(
                {"limits": {"cpu": 2, "memoryMiB": 512}}
            )
        },
    )

    assert (
        _concurrency_function_resources(config, "word-stats-java-lite")["limits"]["cpu"]
        == 4.0
    )
