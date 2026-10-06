"""Prometheus queries used by the load-test report."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sonata_tasks.prometheus import (
    HttpPrometheusClient,
    PrometheusRetryPolicy,
    PrometheusSeries,
)

from nanolab.metrics.interpretation import counter_delta as counter_delta
from nanolab.metrics.interpretation import is_counter as is_counter


def sum_series_by_timestamp(series: tuple[PrometheusSeries, ...]) -> dict[float, float]:
    """Add the samples of `series` that share a timestamp into one map."""
    merged: dict[float, float] = {}
    for item in series:
        for sample in item.samples:
            merged[sample.timestamp] = merged.get(sample.timestamp, 0.0) + sample.value
    return merged


def _client(base_url: str, timeout_seconds: float = 20) -> HttpPrometheusClient:
    return HttpPrometheusClient(
        base_url,
        timeout_seconds=timeout_seconds,
        retry_policy=PrometheusRetryPolicy(attempts=3, backoff_seconds=2),
    )


def query_prometheus_server_time(base_url: str, timeout_seconds: float = 20) -> float:
    """Return Prometheus's current clock as epoch seconds."""
    return _client(base_url, timeout_seconds).server_time()


def query_prometheus_range_series(
    base_url: str,
    metric_name: str,
    start: datetime,
    end: datetime,
    step_seconds: int = 2,
) -> list[dict[str, Any]]:
    """Return `metric_name` samples over the range as sorted timestamp/value dicts."""
    series = _client(base_url).query_range(metric_name, start, end, step_seconds)
    # Gauges retain the summed view used by charts. Counters keep the publisher
    # labels, because summing before reset detection can hide a restart.
    if not is_counter(metric_name):
        merged = sum_series_by_timestamp(series)
        return [
            {
                "timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                "value": merged[timestamp],
            }
            for timestamp in sorted(merged)
        ]
    return sorted(
        [
            {
                "timestamp": datetime.fromtimestamp(sample.timestamp, UTC).isoformat(),
                "value": float(sample.value),
                "labels": dict(item.labels),
            }
            for item in series
            for sample in item.samples
        ],
        key=lambda point: point["timestamp"],
    )
