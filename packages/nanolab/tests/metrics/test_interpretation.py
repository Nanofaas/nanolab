"""Metric boundaries shared by reports and release qualification."""

import json
from pathlib import Path

import pandas as pd
import pytest

from nanolab.tasks.loadtest.prometheus import counter_delta
from nanolab.tasks.loadtest.report import _cards
from nanolab.tasks.loadtest.tasks import _point_stats


def test_overflowed_point_sums_are_unavailable_instead_of_nonfinite_evidence():
    stats = _point_stats([{"timestamp": 1, "value": 1e308}] * 2)
    assert "max" not in stats
    assert stats["invalid_points"] == 1


def test_overflowed_gauge_delta_does_not_hide_valid_observed_bounds():
    stats = _point_stats([{"value": -1e308}, {"value": 1e308}])
    assert stats["min"] == -1e308 and stats["max"] == 1e308
    assert "delta" not in stats


def test_malformed_counter_publisher_metadata_does_not_crash_reports():
    assert (
        counter_delta([{"value": 1, "labels": None}, {"value": 2, "labels": None}])
        is None
    )


@pytest.mark.parametrize(
    ("name", "kind", "expected"),
    [
        ("function_dispatch", None, True),
        ('function_dispatch{function="ordinary"}', None, True),
        ("function_dispatch@worker", None, True),
        ("function_dispatch", "gauge", False),
        ("ordinary_count", "gauge", False),
        ("ordinary_gauge", "counter", True),
        ("ordinary_total", None, True),
        ("ordinary", None, False),
    ],
)
def test_counter_classification_stays_a_product_policy(name, kind, expected):
    from nanolab.metrics.interpretation import is_counter

    assert is_counter(name, kind) is expected


@pytest.mark.parametrize("nested", [False, True])
def test_detailed_report_reads_k6_rate_in_both_export_formats(nested):
    metrics = {
        "http_reqs": {"count": 40, "rate": 4},
        "http_req_failed": {"rate": 0.25, "passes": 10, "fails": 30},
        "http_req_duration": {"p(95)": 12},
    }
    if nested:
        metrics = {name: {"values": value} for name, value in metrics.items()}
    frame = pd.DataFrame(
        {"rejected": [10], "queue_depth": [1], "mean_queue_wait_ms": [2]}
    )
    cards = _cards(frame, {"metrics": metrics}, 20)
    assert "25.000%" in cards
    assert ">40<" in cards
    assert "12.0 ms" in cards


def test_detailed_report_marks_missing_k6_values_unavailable():
    frame = pd.DataFrame(
        {"rejected": [0], "queue_depth": [0], "mean_queue_wait_ms": [0]}
    )
    cards = _cards(frame, {"metrics": {}}, 20)
    assert "0.000%" not in cards
    assert "—" in cards


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", True])
def test_invalid_samples_do_not_become_summary_statistics(value):
    stats = _point_stats([{"value": 3}, {"value": value}], counter=True)
    assert "delta" not in stats
    assert stats["invalid_points"] == 1


def test_empty_counter_is_distinct_from_no_increments():
    assert counter_delta([]) is None
    assert counter_delta([{"value": 5}, {"value": 5}]) == 0


def test_gauge_statistics_follow_timestamp_order_and_can_decrease():
    stats = _point_stats(
        [
            {"timestamp": "2026-10-06T10:00:02Z", "value": 3},
            {"timestamp": "2026-10-06T10:00:01Z", "value": 8},
        ]
    )
    assert stats["first"] == 8
    assert stats["last"] == 3
    assert stats["delta"] == -5


@pytest.mark.parametrize("filename", ["summary-export.json", "handle-summary.json"])
def test_real_k6_exports_agree_across_reports_and_release(tmp_path, filename):
    from nanolab.release.metrics import _k6_value
    from nanolab.tasks.loadtest.comparison_report import read_cell

    summary = json.loads(
        (Path(__file__).parents[1] / "fixtures/k6" / filename).read_text()
    )
    cell = tmp_path / "jvm/run-1"
    cell.mkdir(parents=True)
    (cell / "k6-summary.json").write_text(json.dumps(summary))
    actual = read_cell(tmp_path, "jvm", 1)
    assert actual is not None
    assert actual.requests == 4
    assert actual.failed_rate == 0.25
    assert _k6_value(summary["metrics"], "http_req_failed", "rate", "value") == 0.25
    frame = pd.DataFrame(
        {"rejected": [1], "queue_depth": [0], "mean_queue_wait_ms": [0]}
    )
    assert "25.000%" in _cards(frame, summary, 20)
