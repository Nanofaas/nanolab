"""Observe what the control plane still holds after the traffic stops.

A soak answers a retention question, and retention is only visible once demand is
gone: under load every population is legitimately non-empty, so a leak and a busy
system look the same. This samples the control plane's own metrics across a drain
window long enough to cross the retention bounds under test, so a population that
never falls can be told apart from one that is merely inside its TTL.

It reads the management endpoint and the container's cgroup accounting, and keeps
them apart on purpose: Java heap after a collection, process RSS and cgroup usage
answer different questions, and adding them together answers none.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# The checkpoints the plan names, in seconds after the traffic stops. 30s and 5min
# sit inside the documented retention windows; 30min is past the longest one, which
# is the only point at which "still retained" means "not released".
DEFAULT_CHECKPOINTS_S: tuple[int, ...] = (0, 30, 300, 1800)

# Series whose value is a retained population rather than cumulative work. A counter
# that only ever rises says nothing about retention, and summing it with a gauge
# would produce a number that means nothing at all.
POPULATION_SERIES: tuple[str, ...] = (
    "nanofaas_execution_store_live",
    "nanofaas_execution_store_outcomes",
    "nanofaas_execution_store_keys",
    "nanofaas_execution_store_outcome_bytes",
    "nanofaas_invocation_capacity_executions_reserved",
    "nanofaas_invocation_capacity_input_bytes_reserved",
    "nanofaas_waiter_capacity_reserved",
    "nanofaas_capacity_generations_retiring",
    "nanofaas_replica_snapshot_entries",
    "nanofaas_replica_snapshot_queue_depth",
    "nanofaas_replica_snapshot_active_tasks",
    "nanofaas_deployment_wakeup_pending",
    "nanofaas_http_pending_acquisitions",
    "nanofaas_http_active_connections",
    "nanofaas_http_destination_pools",
    "jvm_memory_used_bytes",
    "jvm_buffer_memory_used_bytes",
    "jvm_threads_live_threads",
    "process_open_fds",
)


def _scrape(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def parse_prometheus(text: str, wanted: tuple[str, ...]) -> dict[str, float]:
    """Return the wanted series from an exposition payload, summing their labels.

    Labelled series are summed per name because the question is how much of a thing
    is retained in total, not per function; a per-function breakdown would grow with
    the name history the campaign is trying to prove bounded. Series absent from the
    payload are simply absent from the result: a profile that never loaded a module
    has no such population, and inventing a zero would claim an observation that was
    never made.
    """
    totals: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        name = head.split("{", 1)[0].strip() or head.strip()
        if name not in wanted:
            continue
        try:
            totals[name] = totals.get(name, 0.0) + float(value)
        except ValueError:
            continue
    return totals


def count_meters(text: str) -> int:
    """Count distinct metric names, which is the meter-registry population.

    An unbounded meter registry is one of the retention failures the campaign is
    about, and its size is not itself exported as a gauge.
    """
    names: set[str] = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head = line.rpartition(" ")[0] or line
        names.add(head.split("{", 1)[0].strip())
    return len(names)


@dataclass
class ObserveDrainTask:
    """Sample retained populations at checkpoints after the load has stopped."""

    task_id: str
    title: str
    management_url: str
    output_path: Path
    checkpoints_s: tuple[int, ...] = DEFAULT_CHECKPOINTS_S
    scrape_timeout_s: float = 10.0
    # Injected so a test drives the clock instead of waiting half an hour.
    sleep: object = field(default=time.sleep)
    now: object = field(default=time.monotonic)

    def run(self) -> Path:
        """Walk the checkpoints, record each sample, and write the observation file."""
        samples: list[dict[str, object]] = []
        started = float(self.now())  # type: ignore[operator]
        previous = 0
        for checkpoint in self.checkpoints_s:
            wait = checkpoint - previous
            if wait > 0:
                self.sleep(wait)  # type: ignore[operator]
            previous = checkpoint
            samples.append(self._sample(checkpoint, started))
        payload = {
            "schema": "nanolab-soak-drain-v1",
            "management_url": self.management_url,
            "checkpoints_s": list(self.checkpoints_s),
            "series": list(POPULATION_SERIES),
            "samples": samples,
            "note": (
                "Populations only. Counters and durations are excluded because "
                "a rising total says nothing about what is still held. Heap, "
                "RSS and cgroup usage are separate observations, never summed."
            ),
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self.output_path.write_text(body)
        return self.output_path

    def _sample(self, checkpoint: int, started: float) -> dict[str, object]:
        sample: dict[str, object] = {
            "checkpoint_s": checkpoint,
            "elapsed_s": round(float(self.now()) - started, 3),  # type: ignore
        }
        try:
            text = _scrape(
                f"{self.management_url}/actuator/prometheus",
                self.scrape_timeout_s,
            )
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            # An unreachable endpoint is recorded as unavailable rather than as empty:
            # "nothing retained" and "could not look" are different answers.
            sample["status"] = "unavailable"
            sample["reason"] = str(error)
            return sample
        sample["status"] = "observed"
        sample["populations"] = parse_prometheus(text, POPULATION_SERIES)
        sample["meter_names"] = count_meters(text)
        return sample
