"""Cgroup samples become working set, CPU and per-window totals, never false zeros."""

import json
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nanolab.tasks.experiment_02.samples import (
    ENGINE_CPU,
    ENGINE_MEMORY,
    OBSERVERS,
    SAMPLER_SCRIPT,
    as_snapshot,
    container_count,
    cpu_cores,
    cpu_seconds,
    memory_byte_seconds,
    oom_kills,
    read_cgroup_samples,
    window_covered,
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
        *((float(at), {".": _group(usage=at * 500_000)}) for at in range(0, 31, 5))
    )
    assert cpu_cores(samples) == [(30.0, 0.5)]


def test_gap_invalidates_a_cpu_rate_window():
    samples = _samples(
        (0.0, {".": _group(usage=0)}),
        (5.0, {".": _group(usage=1_000_000)}),
        (30.0, {".": _group(usage=15_000_000)}),
    )
    assert cpu_cores(samples) == []


def test_counter_reset_invalidates_a_cpu_rate_window():
    usage = [0, 1, 2, 3, 0, 1, 2]
    samples = _samples(
        *(
            (float(i * 5), {".": _group(usage=u * 1_000_000)})
            for i, u in enumerate(usage)
        )
    )
    assert cpu_cores(samples) == []


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


def test_oom_count_uses_attempt_delta_not_previous_cell_total():
    def series(*counts: int) -> list[dict]:
        return _samples(
            *((i * 5.0, {".": _group(oom=c)}) for i, c in enumerate(counts))
        )

    assert oom_kills(series(3, 3)) == 0
    assert oom_kills(series(3, 3, 4)) == 1
    assert oom_kills(series(3, 0)) is None
    assert oom_kills([]) is None


def test_missing_boundary_or_counter_reset_makes_integral_unknown():
    samples = _samples(
        (0.0, {".": _group(memory=100, usage=5_000_000)}),
        (5.0, {".": _group(memory=100, usage=0)}),
        (10.0, {".": _group(memory=100, usage=1_000_000)}),
    )
    assert cpu_seconds(samples, 0.0, 10.0) is None
    assert memory_byte_seconds(samples, 0.0, 10.0) == 1000.0
    assert memory_byte_seconds(samples, 0.0, 11.0) is None
    assert not window_covered(samples, 0.0, 10.0, fields=("cpuUsageUsec",))
    assert window_covered(samples, 0.0, 10.0)
    assert not window_covered(samples, -1.0, 10.0)


def test_observer_cost_excludes_registry_and_deployment():
    samples = _samples(
        (
            0.0,
            {
                ".": _group(memory=1000),
                "@observers": _group(memory=100),
                "registry": _group(memory=500),
            },
        ),
    )
    assert working_set(samples, OBSERVERS) == [(0.0, 100.0)]
    assert working_set(samples) == [(0.0, 1000.0)]


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


def _start_sampler(tmp_path: Path, *extra: str) -> tuple[subprocess.Popen, Path]:
    root = tmp_path / "nanolab-sut.slice"
    _fake_cgroup(root, memory=1000, inactive=200, usage=5)
    out = tmp_path / "samples.jsonl"
    process = subprocess.Popen(
        (sys.executable, "-I", str(SAMPLER_SCRIPT), "--root", str(root),
         "--interval", "30", "--out", str(out), *extra),
    )  # fmt: skip
    deadline = time.monotonic() + 5
    while (time.monotonic() < deadline and not out.exists()) or not out.read_text():
        time.sleep(0.05)
    return process, out


def test_sigterm_flushes_a_final_sample_and_closes_open_files(tmp_path):
    process, out = _start_sampler(tmp_path)
    assert len(out.read_text().splitlines()) == 1
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=5) == 0
    # The interval is 30 s: the second line can only be the final sample.
    assert len(read_cgroup_samples(out).samples) == 2


def test_sampler_records_the_observers_apart(tmp_path):
    observers = tmp_path / "observers"
    _fake_cgroup(observers, memory=100, inactive=20, usage=1)
    root = tmp_path / "nanolab-sut.slice"
    _fake_cgroup(root, memory=1000, inactive=200, usage=5)
    out = tmp_path / "s.jsonl"
    subprocess.run(
        (sys.executable, "-I", str(SAMPLER_SCRIPT), "--root", str(root),
         "--observer-root", str(observers), "--once", "--out", str(out)),
        check=True,
    )  # fmt: skip
    groups = json.loads(out.read_text())["cgroups"]
    assert groups["@observers"]["memoryCurrent"] == 100
    assert groups["."]["memoryCurrent"] == 1000


EXPOSITION = (
    "# TYPE nanofaas_queue_depth gauge\n"
    'nanofaas_queue_depth{function="qr-code-rust"} 3\n'
    'nanofaas_latency_bucket{function="qr-code-rust",le="0.1"} 7\n'
)


class _Metrics(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:
        data = EXPOSITION.encode()
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.mark.parametrize("status", [200, 503])
def test_scheduler_scrape_keeps_the_exposition_raw_and_errors_apart(tmp_path, status):
    handler = type("Handler", (_Metrics,), {"status": status})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/actuator/prometheus"
        root = tmp_path / "nanolab-sut.slice"
        _fake_cgroup(root, memory=1000, inactive=200, usage=5)
        out, scheduler = tmp_path / "s.jsonl", tmp_path / "scheduler.jsonl"
        subprocess.run(
            (sys.executable, "-I", str(SAMPLER_SCRIPT), "--root", str(root), "--once",
             "--out", str(out), "--metrics-url", url,
             "--scheduler-out", str(scheduler)),
            check=True,
        )  # fmt: skip
    finally:
        server.shutdown()
    [line] = [json.loads(text) for text in scheduler.read_text().splitlines()]
    assert len(read_cgroup_samples(out).samples) == 1
    if status == 200:
        assert (line["status"], line["body"]) == ("ok", EXPOSITION)
    else:
        assert (line["status"], line["httpStatus"], line["body"]) == (
            "error",
            503,
            None,
        )


def test_without_scheduler_options_there_is_no_scheduler_file(tmp_path):
    root = tmp_path / "nanolab-sut.slice"
    _fake_cgroup(root, memory=1000, inactive=200, usage=5)
    subprocess.run(
        (sys.executable, "-I", str(SAMPLER_SCRIPT), "--root", str(root), "--once",
         "--out", str(tmp_path / "s.jsonl")),
        check=True,
    )  # fmt: skip
    assert sorted(path.name for path in tmp_path.iterdir() if path.is_file()) == [
        "s.jsonl"
    ]


def test_scheduler_options_must_be_supplied_together(tmp_path):
    root = tmp_path / "nanolab-sut.slice"
    _fake_cgroup(root, memory=1000, inactive=200, usage=5)
    done = subprocess.run(
        (sys.executable, "-I", str(SAMPLER_SCRIPT), "--root", str(root), "--once",
         "--out", str(tmp_path / "s.jsonl"), "--metrics-url", "http://127.0.0.1:1/"),
        capture_output=True,
        check=False,
    )  # fmt: skip
    assert done.returncode == 2
    assert b"go together" in done.stderr
