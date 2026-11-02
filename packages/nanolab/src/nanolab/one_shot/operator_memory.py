"""Whether the operator is swapping, before and during a run.

The epoch loop, the generator and the evidence collector all run on the
operator. When it swaps (another heavy job on the same host), the loop wakes
late, prepares are refused and every later epoch is lost, as in the fourth
phase-1 run. A run refuses to start on a swapping or memory-short operator,
and records the operator's swap traffic and available memory while it runs.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

PAGE_BYTES = 4096
# Swap traffic tolerated while sampling: a few stray pages, not a working set.
MAX_SWAP_PAGES = 256
MIN_AVAILABLE_BYTES = 8 * 1024**3


def _counters(proc: Path) -> dict[str, int]:
    vmstat = dict(
        line.split()[:2] for line in (proc / "vmstat").read_text().splitlines()
    )
    meminfo = {
        line.split(":")[0]: int(line.split()[1]) * 1024
        for line in (proc / "meminfo").read_text().splitlines()
    }
    return {
        "pswpin": int(vmstat["pswpin"]),
        "pswpout": int(vmstat["pswpout"]),
        "available": meminfo["MemAvailable"],
        "swapUsed": meminfo["SwapTotal"] - meminfo["SwapFree"],
    }


def sample(
    seconds: float = 5,
    *,
    proc: Path = Path("/proc"),
    sleep=time.sleep,
) -> dict:
    """Return the swap traffic over `seconds` and the memory left at its end."""
    before = _counters(proc)
    sleep(seconds)
    after = _counters(proc)
    return {
        "atUtc": datetime.now(UTC).isoformat(),
        "seconds": seconds,
        "swappedInPages": after["pswpin"] - before["pswpin"],
        "swappedOutPages": after["pswpout"] - before["pswpout"],
        "memAvailableBytes": after["available"],
        "swapUsedBytes": after["swapUsed"],
    }


def require_quiet(
    measured: dict,
    *,
    max_swap_pages: int = MAX_SWAP_PAGES,
    min_available_bytes: int = MIN_AVAILABLE_BYTES,
) -> None:
    """Refuse an operator that swaps or has too little memory to run on."""
    pages = measured["swappedInPages"] + measured["swappedOutPages"]
    if pages > max_swap_pages:
        raise RuntimeError(
            f"the operator is swapping ({pages} pages in "
            f"{measured['seconds']:g} s): stop other heavy jobs before a run"
        )
    if measured["memAvailableBytes"] < min_available_bytes:
        raise RuntimeError(
            "the operator has "
            f"{measured['memAvailableBytes'] / 1024**3:.1f} GiB available, "
            f"below {min_available_bytes / 1024**3:.0f} GiB: free memory first"
        )


class OperatorMemoryRecorder:
    """Append one operator swap sample per interval to `path` until closed."""

    def __init__(
        self, path: Path, *, interval_seconds: float = 10, proc: Path = Path("/proc")
    ) -> None:
        """Store where and how often to record; nothing runs until `start`."""
        self.path = path
        self.interval_seconds = interval_seconds
        self.proc = proc
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="operator-memory", daemon=True
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                row = sample(
                    self.interval_seconds, proc=self.proc, sleep=self._stop.wait
                )
            except (OSError, KeyError, ValueError) as error:
                row = {"atUtc": datetime.now(UTC).isoformat(), "error": str(error)}
                self._stop.wait(self.interval_seconds)
            with self.path.open("a") as stream:
                stream.write(json.dumps(row) + "\n")

    def start(self) -> None:
        """Begin recording in the background."""
        self._thread.start()

    def close(self) -> None:
        """Stop recording; the last sample may be shorter than the interval."""
        self._stop.set()
        self._thread.join(timeout=30)
