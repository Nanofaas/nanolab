"""The end-to-end budget is a promise about a given amount of work."""

from __future__ import annotations

from nanolab.config import ScenarioConfig
from nanolab.plans.loadtest import end_to_end_p95_budget_ms


def _config(**overrides) -> ScenarioConfig:
    base = {
        "workflow": "loadtest",
        "backend": "container",
        "concurrencyControl": True,
        "functions": ["word-stats-java"],
    }
    base.update(overrides)
    return ScenarioConfig(**base)


def test_the_built_in_text_keeps_the_original_budget() -> None:
    """Every scenario that does not select a corpus must be unchanged."""
    assert end_to_end_p95_budget_ms(_config()) == 50


def test_a_heavier_corpus_gets_a_budget_scaled_to_its_work() -> None:
    """Not a relaxation: the same promise, restated for a request 250x the size.

    Holding 50 ms while multiplying the work would make the check answer "is the
    payload small", not "does the platform keep its promise".
    """
    assert end_to_end_p95_budget_ms(_config(payloadProfile="medium")) > 50
    assert end_to_end_p95_budget_ms(
        _config(payloadProfile="large")
    ) > end_to_end_p95_budget_ms(_config(payloadProfile="medium"))


def test_the_budget_still_leaves_a_regression_room_to_show() -> None:
    """Keep the budget above the measured medium result, but not twice it.

    68.8 ms was measured for medium on two cores; the budget must sit above it
    with headroom, yet below a doubling of it, or it would catch nothing.
    """
    budget = end_to_end_p95_budget_ms(_config(payloadProfile="medium"))
    assert 68.8 < budget < 2 * 68.8
