"""HTTP-backed implementations of the load-test ports."""

from __future__ import annotations

from nanolab.tasks.loadtest.models import TimeWindow
from nanolab.tasks.loadtest.prometheus import (
    query_prometheus_range_series,
    query_prometheus_server_time,
)


class HttpPrometheusClient:
    """Implements PrometheusClient using the Prometheus HTTP API."""

    def __init__(self, url: str) -> None:
        """Remember the Prometheus base URL every query is issued against."""
        self._url = url

    def query_range(
        self,
        expr: str,
        window: TimeWindow,
        step_seconds: int = 5,
    ) -> list[dict[str, float | str]]:
        """Return the `expr` samples inside `window`, one per `step_seconds`."""
        return query_prometheus_range_series(
            self._url, expr, window.start, window.end, step_seconds
        )

    def server_time(self) -> float:
        """Prometheus's current clock (epoch seconds), for window alignment."""
        return query_prometheus_server_time(self._url)
