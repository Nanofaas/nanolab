"""Offline replays of the NanoFaaS forecasters over a run's arrivals."""

import csv
import json

import pytest

from nanolab.tasks.one_shot.forecast_replay import (
    Forecaster,
    ari_path,
    holt_path,
    interval_mean,
    parse_forecaster,
    record_forecast_replay,
)
from tests.one_shot.test_forecast_errors import emitted, manifest, trace

# Computed by TrendForecastSource.holt/ari/mean at Nanofaas/nanofaas 3327c4aa.
SERIES = [
    0.1 * ((i * 7) % 13) + 0.05 * i + (1.5 if i % 5 == 0 else 0) for i in range(80)
]
JAVA_HOLT = [4.4087567822706, 3.813934279684768]
JAVA_HOLT_MEAN = 3.974796853391681
JAVA_ARI = [5.711479556832466, 4.917446727328072]
JAVA_ARI_MEAN = 4.9892585672957335


def test_models_match_the_nanofaas_implementation():
    holt = holt_path(SERIES, 8, 0.4, 0.4, 0.95)
    ari = ari_path(SERIES, 8, 6)
    assert [holt[0], holt[-1]] == pytest.approx(JAVA_HOLT, rel=1e-12)
    assert [ari[0], ari[-1]] == pytest.approx(JAVA_ARI, rel=1e-12)
    assert interval_mean(holt, 10, 25, 85) == pytest.approx(JAVA_HOLT_MEAN, rel=1e-12)
    assert interval_mean(ari, 10, 25, 85) == pytest.approx(JAVA_ARI_MEAN, rel=1e-12)


def test_ari_holds_the_latest_rate_without_history_and_stays_capped():
    assert ari_path([1.0, 2.0, 3.0], 2, 1) == [3.0, 3.0]
    # Doubling changes would reach 32 then 64; the cap holds 2 x 16.
    assert ari_path([1.0, 2.0, 4.0, 8.0, 16.0], 3, 1) == pytest.approx([32] * 3)


def test_forecasters_are_parsed_and_checked_like_nanofaas():
    assert parse_forecaster("holt:trend=0.2") == Forecaster("holt", 10, trend=0.2)
    assert parse_forecaster("ari:window=10,order=12").label == (
        "ari(window=10,order=12,history=360)"
    )
    for wrong in ("arima", "holt:damping=0", "ari:order=180", "ewma:beta=1"):
        with pytest.raises(ValueError, match="forecaster"):
            parse_forecaster(wrong)


def write_run(tmp_path, events):
    (tmp_path / "manifest.json").write_text(manifest().model_dump_json())
    (tmp_path / "trace.json").write_text(trace().model_dump_json())
    (tmp_path / "schedule.json").write_text("[]")
    (tmp_path / "generator.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events)
    )


def test_replay_queries_each_interval_a_lead_before_it(tmp_path):
    # The anchor is a whole multiple of 10 s: each query, 30 s ahead of its
    # interval, sees the windows up to 40-30 s before the start.
    events = [emitted(-35, "edge-1", "f", i) for i in range(5)]
    events += [emitted(25, "edge-1", "f", 10 + i) for i in range(2)]
    write_run(tmp_path, events)
    latest = parse_forecaster("ewma:alpha=1")
    output = tmp_path / "out"
    output.mkdir()
    summary = record_forecast_replay(tmp_path, output, forecasters=[latest], lead=30)
    rows = list(csv.DictReader((output / "forecast-replay.csv").open()))
    assert len(rows) == 2 * 2 * 2
    cell = {(r["epoch"], r["node"], r["function"]): r for r in rows}
    first, second = cell["0", "edge-1", "f"], cell["1", "edge-1", "f"]
    assert (first["model"], first["forecastSource"]) == (latest.label, "replay")
    assert float(first["predictedRate"]) == pytest.approx(0.5)
    assert float(first["error"]) == pytest.approx(0.5 - 2 / 60)
    assert float(second["predictedRate"]) == pytest.approx(0.2)
    assert float(cell["0", "edge-2", "g"]["predictedRate"]) == 0
    model = summary["models"][latest.label]
    assert model["forecastProvider"] == "ewma"
    assert model["overall"]["forecasts"] == 8
    assert summary["leadSeconds"] == 30
    with pytest.raises(FileExistsError):
        record_forecast_replay(tmp_path, output, forecasters=[latest], lead=30)


def test_intervals_before_any_complete_window_have_no_forecast(tmp_path):
    write_run(tmp_path, [emitted(55, "edge-1", "f", 0)])
    output = tmp_path / "out"
    output.mkdir()
    record_forecast_replay(
        tmp_path, output, forecasters=[parse_forecaster("holt")], lead=30
    )
    rows = list(csv.DictReader((output / "forecast-replay.csv").open()))
    assert {r["forecastSource"] for r in rows if r["epoch"] == "0"} == {"no-window"}
    assert {r["forecastSource"] for r in rows if r["epoch"] == "1"} == {"replay"}
