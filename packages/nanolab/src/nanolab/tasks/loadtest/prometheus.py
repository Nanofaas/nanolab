"""Prometheus queries used by the load-test report."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from sonata_tasks.prometheus import (
    HttpPrometheusClient,
    PrometheusRetryPolicy,
    PrometheusSeries,
)


def sum_series_by_timestamp(series: tuple[PrometheusSeries, ...]) -> dict[float, float]:
    """Add the samples of `series` that share a timestamp into one map."""
    merged: dict[float, float] = {}
    for item in series:
        for sample in item.samples:
            merged[sample.timestamp] = merged.get(sample.timestamp, 0.0) + sample.value
    return merged


def is_counter(name: str, metric_type: str | None = None) -> bool:
    """Identify counters by an explicit type or the Prometheus name convention."""
    if metric_type is not None:
        return metric_type == "counter"
    metric = name.split("@", 1)[0].split("{", 1)[0].strip()
    return (
        metric.endswith(("_total", "_count", "_sum")) or metric == "function_dispatch"
    )


def counter_delta(points: list[dict[str, Any]]) -> float:
    """Count increments and resets independently for every labelled series."""
    series: dict[tuple, list[dict[str, Any]]] = {}
    for point in points:
        if "value" in point:
            key = tuple(sorted(point.get("labels", {}).items()))
            series.setdefault(key, []).append(point)
    total = 0.0
    for samples in series.values():
        if all("timestamp" in point for point in samples):
            samples = sorted(samples, key=lambda point: point["timestamp"])
        values = [float(point["value"]) for point in samples]
        for previous, current in pairwise(values):
            total += current - previous if current >= previous else current
    return total


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
