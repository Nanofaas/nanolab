"""Ports the load-test layer depends on, implemented by the HTTP adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from nanolab.tasks.loadtest.models import TimeWindow


class RemoteFileFetcher(Protocol):
    """Pull a file from a remote machine onto the local filesystem."""

    def fetch_from(self, remote: str, local: Path) -> None:
        """Copy the file at `remote` on the far end to local path `local`."""
        ...


class PrometheusClient(Protocol):
    """Read Prometheus range series and clock over its HTTP API."""

    def query_range(
        self,
        expr: str,
        window: TimeWindow,
        step_seconds: int = 5,
    ) -> list[dict[str, float | str]]:
        """Return the `expr` samples inside `window`, one per `step_seconds`."""
        ...
