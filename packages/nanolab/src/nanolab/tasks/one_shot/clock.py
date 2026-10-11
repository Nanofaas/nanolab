"""Keep node clock-health fresh from measured offsets throughout a workflow."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Literal

import httpx
from sonata_engine import Resource, TaskInputs

from nanolab.one_shot.client import NanoFaasOneShotClient
from nanolab.tasks.one_shot.preflight import measure_http_clock


class ClockMonitor:
    """A run-owned refresh loop whose failure invalidates subsequent load."""

    def __init__(
        self,
        refresh: Callable[[], None],
        *,
        interval_seconds: float,
        probe_directory: Path | None = None,
    ):
        """Store the sampler without starting any background work."""
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("clock refresh interval must be positive")
        self.refresh = refresh
        self.probe_directory = probe_directory
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._error: Exception | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        """Verify one fresh sample synchronously before starting the loop."""
        self.refresh()
        self._thread = Thread(target=self._run, name="one-shot-clock", daemon=False)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.refresh()
            except Exception as error:
                self._error = error
                return

    def require_healthy(self) -> None:
        """Fail the consumer when a refresh was rejected or could not be measured."""
        if self._error is not None:
            raise RuntimeError("one-shot clock monitor failed") from self._error

    @property
    def stopping(self) -> bool:
        """Allow a refresh to abandon later nodes during resource release."""
        return self._stop.is_set()

    def close(self) -> None:
        """Stop refreshing before the associated endpoints are torn down."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=40)
            if self._thread.is_alive():
                raise RuntimeError("one-shot clock sampler failed to stop")


def clock_health_resource(
    *,
    endpoints: dict[str, Resource[str]],
    probes: dict[str, Resource[str]],
    output: Path,
    reporting_nodes: Collection[str] | None = None,
    interval_seconds: float = 10,
    max_skew_seconds: float = 0.1,
    protocol_version: Literal[1, 2] = 1,
) -> Resource[ClockMonitor]:
    """Own periodic measured reports and preserve each original measurement time."""
    reports = frozenset(endpoints if reporting_nodes is None else reporting_nodes)
    if not reports <= endpoints.keys():
        raise ValueError("clock reporting nodes outside measured endpoint scope")

    def acquire(inputs: TaskInputs) -> ClockMonitor:
        addresses = [
            (key, inputs.resource(endpoint), inputs.resource(probes[key]))
            for key, endpoint in endpoints.items()
        ]

        def refresh() -> None:
            with httpx.Client(trust_env=False) as http:
                for key, endpoint, probe in addresses:
                    if monitor.stopping:
                        return

                    def record_probe(sample: dict, node: str = key) -> None:
                        raw_output = output.with_name(output.stem + "-probes.jsonl")
                        with raw_output.open("a") as evidence:
                            evidence.write(json.dumps({"node": node, **sample}) + "\n")

                    try:
                        offset, uncertainty, at = measure_http_clock(
                            probe,
                            http=http,
                            repetitions=3,
                            record_probe=record_probe,
                            tolerate_failures=protocol_version == 2,
                        )
                    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                        if protocol_version == 1:
                            raise
                        with output.open("a") as evidence:
                            evidence.write(
                                json.dumps(
                                    {
                                        "node": key,
                                        "status": "MISSING",
                                        "offsetSeconds": None,
                                        "uncertaintySeconds": None,
                                        "measuredAt": None,
                                        "observedAt": datetime.now(UTC).isoformat(),
                                        "errorType": type(error).__name__,
                                        "error": str(error),
                                    }
                                )
                                + "\n"
                            )
                        continue
                    sample = {
                        "node": key,
                        "status": "MEASURED",
                        "offsetSeconds": offset,
                        "uncertaintySeconds": uncertainty,
                        "measuredAt": at.isoformat(),
                    }
                    with output.open("a") as evidence:
                        evidence.write(json.dumps(sample) + "\n")
                    if protocol_version == 2:
                        continue
                    if abs(offset) + uncertainty > max_skew_seconds:
                        raise ValueError("measured clock unhealthy or too uncertain")
                    if key not in reports:
                        continue
                    receipt = NanoFaasOneShotClient(
                        endpoint, http=http, timeout_seconds=2
                    ).update_clock_health(offset_seconds=offset, measured_at=at)
                    if not receipt.healthy:
                        raise ValueError("node rejected measured clock health")

        monitor = ClockMonitor(
            refresh, interval_seconds=interval_seconds, probe_directory=output.parent
        )
        monitor.start()
        return monitor

    return Resource(
        title="Acquire measured periodic one-shot clock health",
        acquire=acquire,
        release=lambda _inputs, monitor: monitor.close(),
        requires=(*endpoints.values(), *probes.values()),
        always_release=True,
    )
