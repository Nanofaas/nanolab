"""Sample a cgroup v2 subtree at a fixed interval; stdlib only, run on the VM.

Usage:
    python3 -I experiment02_cgroup_sampler.py --root DIR --out FILE
        [--observer-root DIR] [--interval SECONDS]
        [--metrics-url URL --scheduler-out FILE] [--once]

Every interval appends one JSON line: the wall-clock time and, for the root and
each child cgroup up to two levels down, the raw counters experiment 02 derives
its series from:

    {"ts": 1761480000.0, "cgroups": {".": {"memoryCurrent": ..., ...},
                                     "docker-ab.scope": {...},
                                     "@observers": {...}}}

`@observers` is the measurement agents' own cgroup, outside the budget: this
process among them, so its cost is on record and never in the deployment's.

The counters are written as the kernel reports them; a file that cannot be
read is `null`, never zero. With `--metrics-url`, the same tick also scrapes the
platform's Prometheus exposition and appends `{"ts", "status", "httpStatus",
"body", "error"}` to the scheduler file, keeping the text whole: labels,
counters and buckets as exposed. A failed scrape is a line with status
`error`; it never costs the cgroup sample its tick.

The first line is written at once, so a caller can wait for it before
starting anything. SIGTERM ends the loop after one last sample, so a window's
end is covered, then closes the files.
"""

from __future__ import annotations

import argparse
import json
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

SCRAPE_TIMEOUT_SECONDS = 1.0


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


def sample(root: Path, observers: Path | None = None) -> dict[str, object]:
    """Return one sample of the root, its children two levels down, the agents."""
    groups: dict[str, object] = {".": sample_cgroup(root)}
    for directory in sorted((*root.glob("*/"), *root.glob("*/*/"))):
        if (directory / "cgroup.procs").exists():
            groups[directory.relative_to(root).as_posix()] = sample_cgroup(directory)
    if observers is not None:
        groups["@observers"] = sample_cgroup(observers)
    return {"ts": time.time(), "cgroups": groups}


def scrape(url: str) -> dict[str, object]:
    """Return one scrape of the exposition at `url`, or why there is none."""
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=SCRAPE_TIMEOUT_SECONDS) as answer:
            return {
                "ts": started,
                "status": "ok",
                "httpStatus": answer.status,
                "body": answer.read().decode("utf-8", errors="replace"),
                "error": None,
            }
    except urllib.error.HTTPError as error:
        return {
            "ts": started,
            "status": "error",
            "httpStatus": error.code,
            "body": None,
            "error": f"HTTP {error.code}",
        }
    except (OSError, ValueError) as error:
        return {
            "ts": started,
            "status": "error",
            "httpStatus": None,
            "body": None,
            "error": f"{type(error).__name__}: {error}",
        }


def _write(stream: IO[str], value: object) -> None:
    stream.write(json.dumps(value, separators=(",", ":")) + "\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    """Sample until SIGTERM, then once more; or once with --once."""
    parser = argparse.ArgumentParser(description="Sample a cgroup v2 subtree.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--observer-root", type=Path)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--metrics-url")
    parser.add_argument("--scheduler-out", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if (args.metrics_url is None) != (args.scheduler_out is None):
        parser.error("--metrics-url and --scheduler-out go together")
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    scheduler = (
        args.scheduler_out.open("a", encoding="utf-8") if args.scheduler_out else None
    )
    try:
        with args.out.open("a", encoding="utf-8") as stream:
            due = time.monotonic()
            while True:
                _write(stream, sample(args.root, args.observer_root))
                if scheduler is not None:
                    _write(scheduler, scrape(args.metrics_url))
                if args.once:
                    return 0
                if stopping:
                    return 0
                due += args.interval
                while not stopping and (delay := due - time.monotonic()) > 0:
                    time.sleep(min(delay, 0.2))
                # Stopping falls through to one last sample: the window ends
                # covered.
    finally:
        if scheduler is not None:
            scheduler.close()


if __name__ == "__main__":
    raise SystemExit(main())
