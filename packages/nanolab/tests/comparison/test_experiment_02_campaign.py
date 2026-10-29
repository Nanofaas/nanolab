"""Experiment 02 aggregates runs, preserving missing values and failed cells."""

import copy

import pytest

from nanolab.comparison.experiment_02 import (
    CONFIGURATIONS_BY_KEY,
    build_experiment_02_matrix,
)
from nanolab.comparison.experiment_02_campaign import aggregate_experiment_02

from .test_experiment_02_summary import BUDGET, PROTOCOL


def _cells(repetitions):
    return build_experiment_02_matrix(
        (CONFIGURATIONS_BY_KEY["direct-reference"],), (BUDGET,), repetitions, seed=1
    )


def _summary(repetition, value=10, *, outcome="measured", sent=100):
    return {
        "configuration": "direct-reference",
        "budget": BUDGET.label,
        "repetition": repetition,
        "outcome": outcome,
        "calendarOriginMs": 1_000_000,
        "reason": "fixture",
        "malformedEvents": 0,
        "anomalies": [],
        "readiness": {"firstCorrectSeconds": 2},
        "phases": [
            {
                "phase": "idle",
                "workingSetBytes": {"mean": 1048576},
                "cpuCores": {"mean": 0.1},
            },
            {
                "phase": "step-01",
                "correctThroughput": value,
                "successFraction": 0.9,
                "sent": sent,
                "correct": sent * 0.9,
                "incorrect": sent * 0.1,
                "p99Ms": value * 2,
                "cpuSecondsPerUseful": None,
                "workingSetBytes": {"mean": 2097152},
            },
        ]
        if outcome == "measured"
        else [],
    }


def test_bootstrap_uses_runs_not_requests_and_keeps_input_order_irrelevant():
    values = [_summary(1, 10, sent=10), _summary(2, 30, sent=100000)]
    result = aggregate_experiment_02(values, _cells(2), PROTOCOL, repetitions=2)
    row = result["reference"][0]
    assert row["configuration"] == "direct-reference"
    assert row["metrics"]["correctThroughput"] == {
        "mean": 20.0,
        "confidence95": [10.0, 30.0],
        "sampleCount": 2,
        "missingRuns": 0,
    }
    assert row["metrics"]["p99Ms"]["mean"] == 40
    assert row["metrics"]["idleMemoryMiB"]["mean"] == 1
    assert row["outcomes"] == {
        "measured": 2,
        "oom": 0,
        "startup_failed": 0,
        "incomplete": 0,
    }
    assert "feasible" not in row
    assert result["bootstrap"]["unit"] == "run"
    assert result == aggregate_experiment_02(
        values[::-1], _cells(2)[::-1], PROTOCOL, repetitions=2
    )


def test_single_run_has_no_interval_and_undefined_cost_stays_null():
    row = aggregate_experiment_02([_summary(1)], _cells(1), PROTOCOL, repetitions=1)[
        "reference"
    ][0]
    assert row["metrics"]["correctThroughput"]["mean"] == 10
    assert row["metrics"]["correctThroughput"]["confidence95"] is None
    assert row["metrics"]["cpuSecondsPerUseful"] == {
        "mean": None,
        "confidence95": None,
        "sampleCount": 0,
        "missingRuns": 1,
    }


def test_failed_and_missing_runs_remain_visible_without_zero_imputation():
    summaries = [
        _summary(1),
        _summary(2, outcome="oom"),
        _summary(3, outcome="startup_failed"),
    ]
    row = aggregate_experiment_02(summaries, _cells(4), PROTOCOL, repetitions=4)[
        "budgetGrid"
    ][0]
    assert row["outcomes"] == {
        "measured": 1,
        "oom": 1,
        "startup_failed": 1,
        "incomplete": 1,
    }
    assert row["expectedRuns"] == 4
    assert row["metrics"]["correctThroughput"] == {
        "mean": 10.0,
        "confidence95": None,
        "sampleCount": 1,
        "missingRuns": 3,
    }


def test_duplicate_or_foreign_repetitions_are_not_combined():
    with pytest.raises(ValueError, match="duplicate"):
        aggregate_experiment_02(
            [_summary(1), _summary(1)], _cells(2), PROTOCOL, repetitions=2
        )
    with pytest.raises(ValueError, match="scope"):
        aggregate_experiment_02([_summary(3)], _cells(2), PROTOCOL, repetitions=2)


@pytest.mark.parametrize(
    "broken",
    [
        {"calendarOriginMs": None},
        {"malformedEvents": 1},
        {"anomalies": ["duplicate send"]},
    ],
)
def test_unusable_request_ledger_is_not_an_empty_successful_measurement(broken):
    summary = copy.deepcopy(_summary(1))
    summary.update(broken)
    with pytest.raises(ValueError, match="ledger"):
        aggregate_experiment_02([summary], _cells(1), PROTOCOL, repetitions=1)
