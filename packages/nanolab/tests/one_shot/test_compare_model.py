"""The model comparison counts the run's routing from where originals executed."""

import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).parents[4] / "scripts/one-shot-compare-model.py"
SPEC = importlib.util.spec_from_file_location("one_shot_compare_model", PATH)
assert SPEC is not None and SPEC.loader is not None
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)

RATES = {"a": {"f": 1.0}, "b": {"f": 0.5}, "cloud": {"f": 0.1}}


def minute(local, granted, cloud):
    """One model result: a serves `local`, grants b `granted`, asks `cloud`."""
    return {
        "context": {"edges": ["a", "b"], "functions": ["f"], "flowQuantum": 0.05},
        "rates": RATES,
        "localUnits": [[local], [10.0]],
        "neighborUnits": [[[0.0], [granted]], [[0.0], [0.0]]],
        "cloudRequestedUnits": [[cloud], [0.0]],
    }


def trace(windows=2):
    return {
        "nodes": ["a", "b", "cloud"],
        "period_seconds": 60,
        "windows": [RATES] * windows,
        "node_roles": {"a": "edge", "b": "edge", "cloud": "cloud-direct"},
    }


def result(
    origin, epoch, *, executed=None, status=200, response: str | None = "success"
):
    return {
        "event": "result",
        "phase": "campaign",
        "origin": origin,
        "epoch": epoch,
        "statusCode": status,
        "responseStatus": response,
        "executionNode": executed,
    }


def test_routing_is_counted_where_originals_executed():
    selected = [minute(10.0, 8.0, 2.0), minute(20.0, 0.0, 0.0)]
    rows = [
        result("a", 0, executed="a"),
        result("a", 0, executed="b"),
        result("a", 0, executed="b"),
        result("a", 0, executed="cloud"),
        result("a", 1, executed="a"),
        result("a", 1, status=429, response=None),
        result("a", 1, status=503, response="error"),
        result("a", 1, status=200, response="error"),
        # Cloud-direct originals are not an edge's routing.
        result("cloud", 0),
    ]
    document = compare.compare(selected, trace(), rows)
    a = next(row for row in document["origins"] if row["origin"] == "a")
    # Units x 0.05 req/s x 60 s: 10 units = 30 requests in a minute.
    assert a["local"]["model"] == 90 and a["local"]["run"] == 2
    assert a["peer"]["model"] == 24 and a["peer"]["run"] == 2
    assert a["cloud"]["model"] == 6 and a["cloud"]["run"] == 1
    assert (a["rejected"], a["failed"]) == (1, 2)
    assert document["routes"] == [
        {
            "origin": "a",
            "destination": "b",
            "model": 24.0,
            "run": 2,
            "relativePercentDifference": pytest.approx((2 - 24) / 13),
        }
    ]
    assert document["series"]["peer"]["model"] == [24.0, 0.0]
    assert document["series"]["peer"]["run"] == [2, 0]
    assert document["series"]["notServed"]["run"] == [0, 3]
    assert document["system"]["notServed"]["run"] == 3


def test_a_model_solved_on_another_load_is_refused():
    selected = [minute(10.0, 0.0, 0.0)]
    with pytest.raises(SystemExit, match="not solved on this run's trace"):
        compare.compare(selected, trace(windows=2), [])


def test_files_are_written_and_a_constant_series_has_no_correlation(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "trace.json").write_text(json.dumps(trace()))
    rows = [result("a", 0, executed="b"), result("a", 1, executed="a")]
    (run / "generator.jsonl").write_text(
        "".join(json.dumps({"msg": json.dumps(row)}) + "\n" for row in rows)
        + "not json\n"
    )
    selected = tmp_path / "selected.json"
    selected.write_text(json.dumps([minute(10.0, 8.0, 0.0)] * 2))
    compare.main([str(run), str(selected), "--output", str(tmp_path / "out")])
    document = json.loads((tmp_path / "out/model-comparison.json").read_text())
    assert document["series"]["cloud"]["correlation"] is None
    assert document["system"]["cloud"]["relativeDifference"] is None
    text = (tmp_path / "out/model-comparison.md").read_text()
    # RPD: (1 - 48) / 24.5.
    assert "| a → b | 48 | 1 | -97.9% | -191.8% |" in text


@pytest.mark.parametrize(
    ("model", "run", "expected"),
    [(100, 150, 0.4), (150, 100, -0.4), (0, 22, 2.0), (22, 0, -2.0), (0, 0, None)],
)
def test_percent_difference_is_symmetric_bounded_and_defined_at_zero(
    model, run, expected
):
    assert compare.percent_difference(model, run) == (
        expected if expected is None else pytest.approx(expected)
    )


def test_load_shares_sum_to_zero_across_destinations(tmp_path):
    selected = [minute(10.0, 8.0, 0.0)] * 2
    rows = [
        result("a", 0, executed="b"),
        result("a", 1, executed="a"),
        result("a", 0, executed="cloud"),
    ]
    document = compare.compare(selected, trace(windows=2), rows)
    row = next(r for r in document["origins"] if r["origin"] == "a")
    total = sum(row[k]["loadShareDifference"] for k in ("local", "peer", "cloud"))
    total += row["notServedShare"]
    assert row["load"] == 3
    assert total == pytest.approx(
        (3 - sum(row[k]["model"] for k in ("local", "peer", "cloud"))) / 3
    )
    assert compare.points(0.0099) == "+0.99 pp"
