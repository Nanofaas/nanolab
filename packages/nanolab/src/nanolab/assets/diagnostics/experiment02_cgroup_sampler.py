"""Sample a cgroup v2 subtree at a fixed interval; stdlib only, run on the VM.

Usage:
    python3 -I experiment02_cgroup_sampler.py --root DIR --out FILE
        [--interval SECONDS] [--once]

Every interval appends one JSON line: the wall-clock time and, for the root and
each child cgroup up to two levels down, the raw counters experiment 02 derives
its series from:

    {"ts": 1761480000.0, "cgroups": {".": {"memoryCurrent": ..., ...},
                                     "docker-ab.scope": {...}}}

The counters are written as the kernel reports them. The working set, the CPU
rate and every per-window total are computed afterwards, so a sample that is
missing stays missing instead of becoming a zero. SIGTERM ends the loop after
the line being written, never in the middle of one.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_keyed(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            values[key] = int(value)
    return values


def sample_cgroup(directory: Path) -> dict[str, int | None]:
    """Return one cgroup's memory, CPU and OOM counters."""
    memory = _read_keyed(directory / "memory.stat")
    cpu = _read_keyed(directory / "cpu.stat")
    events = _read_keyed(directory / "memory.events")
    return {
        "memoryCurrent": _read_int(directory / "memory.current"),
        "inactiveFile": memory.get("inactive_file"),
        "cpuUsageUsec": cpu.get("usage_usec"),
        "nrThrottled": cpu.get("nr_throttled"),
        "throttledUsec": cpu.get("throttled_usec"),
        "oomKill": events.get("oom_kill"),
    }


def sample(root: Path) -> dict[str, object]:
    """Return one sample of the root cgroup and its children, two levels down."""
    groups: dict[str, object] = {".": sample_cgroup(root)}
    for directory in sorted((*root.glob("*/"), *root.glob("*/*/"))):
        if (directory / "cgroup.procs").exists():
            groups[directory.relative_to(root).as_posix()] = sample_cgroup(directory)
    return {"ts": time.time(), "cgroups": groups}


def main(argv: list[str] | None = None) -> int:
    """Sample until SIGTERM, or once with --once."""
    parser = argparse.ArgumentParser(description="Sample a cgroup v2 subtree.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with args.out.open("a", encoding="utf-8") as stream:
        due = time.monotonic()
        while True:
            stream.write(json.dumps(sample(args.root), separators=(",", ":")) + "\n")
            stream.flush()
            if args.once:
                return 0
            due += args.interval
            while not stopping and (delay := due - time.monotonic()) > 0:
                time.sleep(min(delay, 0.2))
            if stopping:
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
