"""Series derived from the cgroup sampler: working set, CPU rate, window totals.

The definitions are the paper's (§4.3), applied to the budget's cgroup rather
than to cAdvisor's view of it. Memory is the working set, `memory.current` less
`inactive_file`. CPU is the cumulative `usage_usec`, reported in cores as a rate
over the 30 s before each sample. Per-response efficiency needs totals over a
window instead: CPU seconds from the cumulative counter, and memory occupancy as
the time integral of the working set.

A window the samples do not cover, or one with a gap longer than two
intervals, has no total: `None`, never zero. A sampler that died for a minute
did not see a minute of zero memory.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from nanolab.tasks.experiment_02.budget import ENGINE_CGROUPS
from nanolab.workspace.paths import bundled_assets_root

SAMPLER_SCRIPT = (
    bundled_assets_root() / "diagnostics" / "experiment02_cgroup_sampler.py"
)
SAMPLE_INTERVAL_SECONDS = 5.0
CPU_WINDOW_SECONDS = 30.0
DEPLOYMENT_MEMORY = "deployment_working_set_bytes"
DEPLOYMENT_CPU = "deployment_cpu_cores"
ENGINE_MEMORY = "engine_working_set_bytes"
ENGINE_CPU = "engine_cpu_cores"
ROOT: tuple[str, ...] = (".",)

Samples = Sequence[Mapping[str, Any]]
Series = list[tuple[float, float]]


@dataclass(frozen=True)
class CgroupSamples:
    """The parsed samples of one run, and how many lines could not be read."""

    samples: list[dict[str, Any]]
    malformed: int


def read_cgroup_samples(path: Path) -> CgroupSamples:
    """Read the sampler's lines, counting rather than guessing a broken one."""
    samples: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            sample = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if (
            isinstance(sample, dict)
            and isinstance(sample.get("ts"), int | float)
            and isinstance(sample.get("cgroups"), dict)
        ):
            samples.append(sample)
        else:
            malformed += 1
    return CgroupSamples(samples, malformed)


def _sum(samples: Samples, cgroups: tuple[str, ...], *fields: str) -> Series:
    """Sum `fields` combined over `cgroups`, skipping samples that lack any."""
    points: Series = []
    for sample in samples:
        groups = sample["cgroups"]
        total = 0.0
        for name in cgroups:
            values = groups.get(name)
            if values is None or any(values.get(field) is None for field in fields):
                break
            if len(fields) == 2:
                total += max(0, values[fields[0]] - values[fields[1]])
            else:
                total += values[fields[0]]
        else:
            points.append((float(sample["ts"]), total))
    return points


def working_set(samples: Samples, cgroups: tuple[str, ...] = ROOT) -> Series:
    """Return the working set of `cgroups` combined, in bytes."""
    return _sum(samples, cgroups, "memoryCurrent", "inactiveFile")


def cpu_cores(
    samples: Samples,
    cgroups: tuple[str, ...] = ROOT,
    window_seconds: float = CPU_WINDOW_SECONDS,
) -> Series:
    """Return the CPU rate of `cgroups`, in cores, over the window before each sample.

    The rate starts from the latest sample at least `window_seconds` earlier; a
    sample with no such predecessor, or after a counter reset, has no rate.
    """
    usage = _sum(samples, cgroups, "cpuUsageUsec")
    times = [at for at, _ in usage]
    points: Series = []
    for index, (at, value) in enumerate(usage):
        earlier = bisect_right(times, at - window_seconds, hi=index) - 1
        if earlier < 0:
            continue
        start, start_value = usage[earlier]
        if value < start_value:
            continue
        points.append((at, (value - start_value) / (at - start) / 1e6))
    return points


def _covered(series: Series, start: float, end: float, interval: float) -> bool:
    """Whether samples bracket the window with no gap longer than two intervals."""
    times = [at for at, _ in series]
    first = bisect_right(times, start) - 1
    last = bisect_left(times, end)
    if first < 0 or last >= len(times):
        return False
    return all(
        times[index + 1] - times[index] <= 2 * interval for index in range(first, last)
    )


def _at(series: Series, at: float) -> float:
    """Interpolate a covered series linearly at `at`."""
    times = [point for point, _ in series]
    index = bisect_left(times, at)
    if times[index] == at:
        return series[index][1]
    (before, low), (after, high) = series[index - 1], series[index]
    return low + (high - low) * (at - before) / (after - before)


def cpu_seconds(
    samples: Samples,
    start: float,
    end: float,
    cgroups: tuple[str, ...] = ROOT,
    interval: float = SAMPLE_INTERVAL_SECONDS,
) -> float | None:
    """Return the CPU time `cgroups` used in the window, or `None` if unknown."""
    usage = _sum(samples, cgroups, "cpuUsageUsec")
    if not _covered(usage, start, end, interval):
        return None
    return (_at(usage, end) - _at(usage, start)) / 1e6


def memory_byte_seconds(
    samples: Samples,
    start: float,
    end: float,
    cgroups: tuple[str, ...] = ROOT,
    interval: float = SAMPLE_INTERVAL_SECONDS,
) -> float | None:
    """Return the working set integrated over the window, or `None` if unknown."""
    memory = working_set(samples, cgroups)
    if not _covered(memory, start, end, interval):
        return None
    inside = [(at, value) for at, value in memory if start < at < end]
    points = [(start, _at(memory, start)), *inside, (end, _at(memory, end))]
    return sum(
        (later - earlier) * (low + high) / 2
        for (earlier, low), (later, high) in pairwise(points)
    )


def oom_kills(samples: Samples, cgroups: tuple[str, ...] = ROOT) -> int:
    """Return how many processes the OOM killer took during the samples."""
    kills = _sum(samples, cgroups, "oomKill")
    if not kills:
        return 0
    return int(kills[-1][1] - kills[0][1])


def container_count(samples: Samples) -> list[tuple[float, int]]:
    """Return how many container scopes exist at each sample: the live instances."""
    return [
        (
            float(sample["ts"]),
            sum(
                1
                for name in sample["cgroups"]
                if name.rsplit("/", 1)[-1].startswith("docker-")
                and name.endswith(".scope")
            ),
        )
        for sample in samples
    ]


def _points(series: Series) -> list[dict[str, object]]:
    return [
        {"timestamp": datetime.fromtimestamp(at, UTC).isoformat(), "value": value}
        for at, value in series
    ]


def as_snapshot(samples: Samples) -> dict[str, Any]:
    """Return the series in the shape of a Prometheus snapshot.

    `summarize_phases` reads that shape; giving it the same shape lets
    experiment 02 reuse experiment 01's per-phase summaries unchanged.
    """
    return {
        "queries": {
            DEPLOYMENT_MEMORY: {"points": _points(working_set(samples)), "error": None},
            DEPLOYMENT_CPU: {"points": _points(cpu_cores(samples)), "error": None},
            ENGINE_MEMORY: {
                "points": _points(working_set(samples, ENGINE_CGROUPS)),
                "error": None,
            },
            ENGINE_CPU: {
                "points": _points(cpu_cores(samples, ENGINE_CGROUPS)),
                "error": None,
            },
        }
    }
