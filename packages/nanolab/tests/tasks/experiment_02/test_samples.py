"""Cgroup samples become working set, CPU and per-window totals, never false zeros."""

import json
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from nanolab.tasks.experiment_02.samples import (
    ENGINE_CPU,
    ENGINE_MEMORY,
    SAMPLER_SCRIPT,
    as_snapshot,
    container_count,
    cpu_cores,
    cpu_seconds,
    memory_byte_seconds,
    oom_kills,
    read_cgroup_samples,
    working_set,
)


def _group(
    memory: int = 0, inactive: int = 0, usage: int = 0, oom: int = 0
) -> dict[str, int]:
    return {
        "memoryCurrent": memory,
        "inactiveFile": inactive,
        "cpuUsageUsec": usage,
        "nrThrottled": 0,
        "throttledUsec": 0,
        "oomKill": oom,
    }


def _samples(*points: tuple[float, dict[str, dict[str, int]]]) -> list[dict]:
    return [{"ts": ts, "cgroups": groups} for ts, groups in points]


def _fake_cgroup(directory: Path, *, memory: int, inactive: int, usage: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cgroup.procs").write_text("")
    (directory / "memory.current").write_text(f"{memory}\n")
    (directory / "memory.stat").write_text(f"anon 1\ninactive_file {inactive}\n")
    (directory / "cpu.stat").write_text(
        f"usage_usec {usage}\nnr_throttled 3\nthrottled_usec 40\n"
    )
    (directory / "memory.events").write_text("oom 0\noom_kill 1\n")


def test_sampler_reads_a_cgroup_tree_once(tmp_path):
    root = tmp_path / "nanolab-sut.slice"
    _fake_cgroup(root, memory=1000, inactive=200, usage=5)
    _fake_cgroup(root / "docker-ab.scope", memory=300, inactive=0, usage=2)
    (root / "not-a-cgroup").mkdir()
    out = tmp_path / "samples.jsonl"
    subprocess.run(
        (sys.executable, "-I", str(SAMPLER_SCRIPT), "--root", str(root), "--once",
         "--out", str(out)),
        check=True,
    )  # fmt: skip
    line = json.loads(out.read_text())
    assert set(line["cgroups"]) == {".", "docker-ab.scope"}
    assert line["cgroups"]["."] == {
        "memoryCurrent": 1000,
        "inactiveFile": 200,
        "cpuUsageUsec": 5,
        "nrThrottled": 3,
        "throttledUsec": 40,
        "oomKill": 1,
    }


def test_sampler_stops_cleanly_on_sigterm(tmp_path):
    root = tmp_path / "nanolab-sut.slice"
    _fake_cgroup(root, memory=1000, inactive=200, usage=5)
    out = tmp_path / "samples.jsonl"
    process = subprocess.Popen(
        (sys.executable, "-I", str(SAMPLER_SCRIPT), "--root", str(root),
         "--interval", "0.1", "--out", str(out)),
    )  # fmt: skip
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (
        not out.exists() or len(out.read_text().splitlines()) < 2
    ):
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=5) == 0
    samples = read_cgroup_samples(out)
    assert samples.malformed == 0
    assert len(samples.samples) >= 2


def test_working_set_subtracts_inactive_file():
    samples = _samples((0.0, {".": _group(memory=1000, inactive=200)}))
    assert working_set(samples) == [(0.0, 800.0)]


def test_engine_series_add_docker_and_containerd():
    samples = _samples(
        (0.0, {".": _group(), "docker.service": _group(memory=50),
               "containerd.service": _group(memory=30)}),
    )  # fmt: skip
    assert working_set(samples, ("docker.service", "containerd.service")) == [
        (0.0, 80.0)
    ]


def test_cpu_cores_over_a_thirty_second_window():
    samples = _samples(
        (0.0, {".": _group(usage=0)}),
        (5.0, {".": _group(usage=1_000_000)}),
        (30.0, {".": _group(usage=15_000_000)}),
    )
    assert cpu_cores(samples) == [(30.0, 0.5)]


def test_cpu_seconds_and_memory_byte_seconds_over_a_window():
    samples = _samples(
        (0.0, {".": _group(memory=100, usage=0)}),
        (5.0, {".": _group(memory=300, usage=2_000_000)}),
        (10.0, {".": _group(memory=300, usage=4_000_000)}),
    )
    assert cpu_seconds(samples, 0.0, 10.0) == 4.0
    assert cpu_seconds(samples, 2.5, 7.5) == 2.0
    assert memory_byte_seconds(samples, 0.0, 10.0) == 2500.0


def test_gap_makes_the_window_unknown_not_zero():
    samples = _samples(
        (0.0, {".": _group(memory=100, usage=0)}),
        (5.0, {".": _group(memory=100, usage=1)}),
        (30.0, {".": _group(memory=100, usage=2)}),
    )
    assert cpu_seconds(samples, 0.0, 30.0) is None
    assert memory_byte_seconds(samples, 0.0, 30.0) is None
    assert cpu_seconds(samples, 0.0, 40.0) is None


def test_truncated_last_line_is_counted_not_parsed(tmp_path):
    path = tmp_path / "samples.jsonl"
    path.write_text('{"ts": 0, "cgroups": {}}\n{"ts": 5, "cgro')
    samples = read_cgroup_samples(path)
    assert len(samples.samples) == 1
    assert samples.malformed == 1


def test_oom_kill_count():
    samples = _samples(
        (0.0, {".": _group(oom=2)}),
        (5.0, {".": _group(oom=2)}),
        (10.0, {".": _group(oom=3)}),
    )
    assert oom_kills(samples) == 1
    assert oom_kills(samples[:2]) == 0


def test_container_count_follows_the_scopes():
    samples = _samples(
        (0.0, {".": _group(), "docker.service": _group(), "docker-a.scope": _group()}),
        (5.0, {".": _group(), "docker-a.scope": _group(), "docker-b.scope": _group()}),
    )
    assert container_count(samples) == [(0.0, 1), (5.0, 2)]


def test_snapshot_carries_deployment_and_engine_series():
    samples = _samples(
        (0.0, {".": _group(memory=100), "docker.service": _group(memory=10),
               "containerd.service": _group(memory=5)}),
    )  # fmt: skip
    snapshot = as_snapshot(samples)
    point = snapshot["queries"][ENGINE_MEMORY]["points"][0]
    assert point["value"] == 15.0
    assert datetime.fromisoformat(point["timestamp"]).timestamp() == 0.0
    assert snapshot["queries"][ENGINE_CPU]["points"] == []
