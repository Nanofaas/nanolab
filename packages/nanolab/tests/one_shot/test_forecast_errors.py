"""Forecast errors per interval, edge node and function, from a run's own files."""

import csv
import json

import pytest

from nanolab.one_shot.models import CampaignManifest
from nanolab.tasks.one_shot.forecast_errors import record_forecast_errors, solver_rate
from nanolab.tasks.one_shot.trace import TraceArtifact

ANCHOR = 1_793_000_000.0  # 2026-10-26T07:33:20Z, the manifest anchor.


def manifest():
    return CampaignManifest(
        provider="multipass",
        purpose="workflow-validation",
        environment_fingerprint="sha256:" + "a" * 64,
        source_commit="a" * 40,
        profile_sha256="b" * 64,
        qualification_sha256="c" * 64,
        trace_sha256="d" * 64,
        seed=1,
        nodes=["edge-1", "edge-2", "cloud"],
        functions=["f", "g"],
        period_seconds=60,
        lead_seconds=90,
        flow_quantum=0.05,
        modes=["ewma"],
        mode="ewma",
        anchor="2026-10-26T07:33:20+00:00",
        parameters={
            "nodes": [
                {"id": "edge-1", "kind": "edge"},
                {"id": "edge-2", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ]
        },
    )


def trace():
    window = {node: {"f": 0.1, "g": 0.05} for node in ("edge-1", "edge-2", "cloud")}
    return TraceArtifact(
        nodes=["edge-1", "edge-2", "cloud"],
        functions=["f", "g"],
        period_seconds=60,
        flow_quantum=0.05,
        seed=1,
        payloads={"f": {}, "g": {}},
        windows=[window, window],
    )


def emitted(at, origin, function, index):
    row = {
        "originalId": f"{origin}:{function}:{index}",
        "origin": origin,
        "function": function,
        "event": "emitted",
        "emittedAt": ANCHOR + at,
    }
    return {"msg": json.dumps(row)}


def plan(**rates):
    return {
        "result": {
            "status": "DEGRADED",
            "plan": {
                "flowQuantum": 0.05,
                "functions": {
                    name: {"forecastRate": rate} for name, rate in rates.items()
                },
            },
        }
    }


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "manifest.json").write_text(manifest().model_dump_json())
    (tmp_path / "trace.json").write_text(trace().model_dump_json())
    schedule = [
        {"phase": "warmup", "epoch": -1, "origin": "edge-1", "function": "f"},
        *(
            {"phase": "campaign", "epoch": 0, "origin": "edge-1", "function": "f"}
            for _ in range(6)
        ),
    ]
    (tmp_path / "schedule.json").write_text(json.dumps(schedule))
    events = [
        emitted(-30, "edge-1", "f", "warmup"),  # Before the first interval.
        *(emitted(second, "edge-1", "f", second) for second in range(1, 7)),
        emitted(59.999, "edge-1", "f", "late"),
        emitted(59.999, "edge-1", "f", "late"),  # A repeated record counts once.
        emitted(60, "edge-1", "f", "boundary"),  # Starts the second interval.
        *(emitted(10 + second, "edge-1", "g", second) for second in range(3)),
        {"msg": json.dumps({"event": "result", "originalId": "edge-1:f:1"})},
    ]
    (tmp_path / "generator.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in events)
    )
    epochs = [
        {
            "epoch": 0,
            "nodes": {"edge-1": plan(f=0.123, g=0.0), "edge-2": {"result": None}},
        }
    ]
    (tmp_path / "epochs.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in epochs)
    )
    return tmp_path


def test_rows_compare_the_auction_forecast_with_the_emitted_load(run_dir, tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    summary = record_forecast_errors(run_dir, output)
    rows = {
        (int(row["epoch"]), row["node"], row["function"]): row
        for row in csv.DictReader((output / "forecast-errors.csv").open())
    }
    # Two intervals, two edge nodes, two functions; the cloud runs no auction.
    assert len(rows) == 8
    over = rows[0, "edge-1", "f"]
    assert over["forecastSource"] == "plan"
    assert float(over["predictedRate"]) == 0.123
    assert float(over["solverRate"]) == 0.1
    assert int(over["actualRequests"]) == 7
    assert int(over["scheduledRequests"]) == 6
    assert float(over["error"]) == pytest.approx(0.123 - 7 / 60)
    assert float(over["relativeError"]) == pytest.approx((0.123 - 7 / 60) / (7 / 60))
    assert float(over["errorVsOracle"]) == pytest.approx(0.023)
    under = rows[0, "edge-1", "g"]
    assert float(under["error"]) == pytest.approx(-0.05)
    assert float(under["absoluteError"]) == pytest.approx(0.05)
    assert [rows[0, "edge-2", name]["forecastSource"] for name in "fg"] == [
        "no-plan",
        "no-plan",
    ]
    assert rows[0, "edge-2", "f"]["error"] == ""
    assert rows[1, "edge-1", "f"]["forecastSource"] == "no-epoch"
    assert int(rows[1, "edge-1", "f"]["actualRequests"]) == 1
    overall = summary["overall"]
    assert (overall["cells"], overall["forecasts"]) == (8, 2)
    assert overall["overestimates"]["count"] == 1
    assert overall["underestimates"]["count"] == 1
    assert overall["underestimates"]["largestError"] == pytest.approx(-0.05)
    assert overall["actualRequests"] == 10
    assert overall["predictedRequests"] == pytest.approx(0.123 * 60)
    assert summary["byNodeFunction"]["edge-2"]["f"]["meanError"] is None
    assert summary["forecastProvider"] == "ewma"
    assert json.loads((output / "forecast-errors.json").read_text()) == summary


def test_results_are_never_overwritten(run_dir):
    record_forecast_errors(run_dir)
    with pytest.raises(FileExistsError):
        record_forecast_errors(run_dir)


@pytest.mark.parametrize(
    ("rate", "expected"), [(0.123, 0.1), (0.15, 0.15), (0.049, 0.0), (1.0, 1.0)]
)
def test_solver_rate_floors_on_the_decimal_grid(rate, expected):
    # Binary floating point would floor 0.15 / 0.05 to 2.
    assert solver_rate(rate, 0.05) == expected
