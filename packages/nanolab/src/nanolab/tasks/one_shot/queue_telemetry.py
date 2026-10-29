"""Independent one-second queue scrapes; failures remain observations, never zeros."""

from __future__ import annotations

import json
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any

import httpx

from nanolab.tasks.one_shot.artifacts import append_observation

# Preserve labels: the empty function label is a global series, not another function.
METRIC_PREFIXES = (
    "sync_queue_",
    "function_queue_",
    "function_enqueue_",
    "function_in_flight",
    "function_latency_",
    "function_e2e_latency_",
    "execution_in_flight_",
)
SAMPLE = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)=("(?:[^"\\]|\\.)*")(?:,|$)')


def queue_samples(text: str) -> list[dict[str, Any]]:
    """Read the selected Prometheus series without summing away function/reason."""
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = SAMPLE.fullmatch(line)
        if match is None:
            raise ValueError("malformed Prometheus sample")
        name, encoded_labels, encoded_value = match.groups()
        if name != "process_start_time_seconds" and not name.startswith(
            METRIC_PREFIXES
        ):
            continue
        labels = {}
        position = 0
        for label in LABEL.finditer(encoded_labels or ""):
            if label.start() != position:
                raise ValueError("malformed Prometheus labels")
            labels[label[1]] = json.loads(label[2])
            position = label.end()
        if position != len(encoded_labels or ""):
            raise ValueError("malformed Prometheus labels")
        value = float(encoded_value)
        rows.append(
            {
                "name": name,
                "labels": labels,
                "value": value if math.isfinite(value) else None,
            }
        )
    return rows


class QueueTelemetryCollector:
    """Own one bounded HTTP worker per node, independent of physical-proof scans."""

    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        endpoints: dict[str, str],
        *,
        http: httpx.Client | None = None,
    ):
        """Start workers after resolving the owned endpoints."""
        self.stop = Event()
        self.errors: list[Exception] = []
        self.threads: list[Thread] = []
        self.run_dir, self.run_id = run_dir, run_id
        try:
            for node, endpoint in endpoints.items():
                thread = Thread(
                    target=self.loop,
                    args=(node, endpoint, http),
                    name=f"queue-sampler-{node}",
                )
                thread.start()
                self.threads.append(thread)
        except Exception:
            self.close()
            raise

    def loop(self, node: str, endpoint: str, http: httpx.Client | None = None) -> None:
        """Skip overdue slots explicitly; never issue catch-up bursts after a stall."""
        owned = http is None
        client = http or httpx.Client(trust_env=False, timeout=0.8)
        scheduled = time.monotonic()
        try:
            while not self.stop.wait(max(0, scheduled - time.monotonic())):
                started = time.monotonic()
                skipped = max(0, math.floor(started - scheduled))
                scheduled += skipped
                row: dict[str, Any] = {
                    "runId": self.run_id,
                    "node": node,
                    "atUtc": datetime.now(UTC).isoformat(),
                    "scheduledMonotonicSeconds": scheduled,
                    "startedMonotonicSeconds": started,
                    "lateSeconds": max(0, started - scheduled),
                    "skippedPeriods": skipped,
                }
                try:
                    response = client.get(
                        endpoint.replace(":8080", ":9090").rstrip("/")
                        + "/actuator/prometheus",
                        timeout=0.8,
                    )
                    response.raise_for_status()
                    row["samples"] = queue_samples(response.text)
                    row["measurementComplete"] = any(
                        sample["name"] == "sync_queue_depth"
                        for sample in row["samples"]
                    )
                except (httpx.HTTPError, ValueError) as error:
                    row["measurementComplete"] = False
                    row["collectionError"] = str(error)
                row["finishedMonotonicSeconds"] = time.monotonic()
                append_observation(self.run_dir / f"queue-samples-{node}.jsonl", row)
                scheduled += 1
        except Exception as error:
            self.errors.append(error)
        finally:
            if owned:
                client.close()

    def require_healthy(self) -> None:
        """Disk/thread failures abort evidence collection; HTTP failures are saved."""
        if self.errors:
            raise RuntimeError("queue telemetry collector failed") from self.errors[0]

    def close(self) -> None:
        """Stop and join all workers before the runtime endpoints disappear."""
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("queue telemetry cleanup unconfirmed")
        self.require_healthy()


def queue_timings(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum queued intervals per attempt; missing boundaries stay unmeasurable."""
    grouped: dict[tuple, list[dict[str, Any]]] = {}
    for row in events:
        event = row["attributes"]
        key = (event["processId"], event["executionId"], event["attempt"])
        grouped.setdefault(key, []).append(event)
    results = []
    for (process, execution, attempt), rows in grouped.items():
        rows.sort(key=lambda event: event["monotonicNanos"])
        start = None
        total = 0
        complete = rows[0]["event"] == "QUEUED"
        outcome = "waiting"
        for event in rows:
            stamp, stage = event["monotonicNanos"], event["event"]
            if stage in ("QUEUED", "RETURNED"):
                complete = complete and start is None
                start = stamp
                outcome = "waiting"
            elif stage in ("DISPATCH", "EXPIRED", "REMOVED"):
                if start is None:
                    complete = False
                else:
                    total += stamp - start
                start = None
                outcome = stage.lower()
            else:
                complete = False
        complete = complete and start is None
        results.append(
            {
                "processId": process,
                "executionId": execution,
                "attempt": attempt,
                "originalId": rows[0].get("originalId"),
                "function": rows[0]["function"],
                "queueWaitSeconds": total / 1e9 if complete else None,
                "queueOutcome": outcome,
                "measurementComplete": complete,
            }
        )
    return results
