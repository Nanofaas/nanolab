"""A run refuses a swapping operator and records its swap traffic."""

import json
import time
from pathlib import Path

import pytest

from nanolab.one_shot import operator_memory

GIB = 1024**3


def write_proc(proc: Path, *, pswpin, pswpout, available_kib, swap_free_kib):
    proc.mkdir(exist_ok=True)
    (proc / "vmstat").write_text(
        f"nr_free_pages 1\npswpin {pswpin}\npswpout {pswpout}\n"
    )
    (proc / "meminfo").write_text(
        "MemTotal: 100000000 kB\n"
        f"MemAvailable: {available_kib} kB\n"
        "SwapTotal: 8000000 kB\n"
        f"SwapFree: {swap_free_kib} kB\n"
    )


def test_a_sample_counts_the_swap_traffic_over_its_interval(tmp_path):
    proc = tmp_path / "proc"
    write_proc(
        proc, pswpin=10, pswpout=20, available_kib=50_000_000, swap_free_kib=8_000_000
    )

    def sleep(_seconds):
        write_proc(
            proc,
            pswpin=15,
            pswpout=320,
            available_kib=40_000_000,
            swap_free_kib=7_000_000,
        )

    measured = operator_memory.sample(5, proc=proc, sleep=sleep)
    assert (measured["swappedInPages"], measured["swappedOutPages"]) == (5, 300)
    assert measured["memAvailableBytes"] == 40_000_000 * 1024
    assert measured["swapUsedBytes"] == 1_000_000 * 1024
    with pytest.raises(RuntimeError, match="305 pages in 5 s"):
        operator_memory.require_quiet(measured)


def test_old_swapped_pages_alone_do_not_refuse_a_run():
    measured = {
        "seconds": 5,
        "swappedInPages": 3,
        "swappedOutPages": 0,
        "memAvailableBytes": 64 * GIB,
        "swapUsedBytes": 4 * GIB,
    }
    operator_memory.require_quiet(measured)
    with pytest.raises(RuntimeError, match=r"2\.0 GiB available"):
        operator_memory.require_quiet({**measured, "memAvailableBytes": 2 * GIB})


def test_the_recorder_appends_samples_until_closed(tmp_path):
    proc = tmp_path / "proc"
    write_proc(proc, pswpin=1, pswpout=1, available_kib=9, swap_free_kib=8_000_000)
    path = tmp_path / "operator-memory.jsonl"
    recorder = operator_memory.OperatorMemoryRecorder(
        path, interval_seconds=0.05, proc=proc
    )
    recorder.start()
    deadline = time.monotonic() + 5
    while not path.exists() or len(path.read_text().splitlines()) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    (proc / "vmstat").unlink()
    time.sleep(0.2)
    recorder.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["swappedOutPages"] == 0 and rows[0]["memAvailableBytes"] == 9216
    assert any("error" in row for row in rows)
