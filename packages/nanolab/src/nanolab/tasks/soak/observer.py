"""One continuous observer with immediate persistence and no historical buffers."""

import math
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from threading import Event, Lock, Thread

from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.models import Phase
from nanolab.tasks.soak.ports import Clock, ProcessProbe

_PHASES = frozenset(
    ("preflight", "warmup", "baseline", "steady", "drain", "diagnostic")
)


def _positive(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("expected positive finite duration")


def sample_deadlines(
    start_s: float, end_s: float, interval_s: float
) -> Iterator[float]:
    """Yield the absolute deadlines of every sample in a window.

    Absolute rather than cumulative, so a slow scrape delays one sample
    instead of shifting every one that follows it.
    """
    _positive(interval_s)
    if not math.isfinite(start_s) or not math.isfinite(end_s) or end_s < start_s:
        raise ValueError("invalid observation window")
    index = 0
    while True:
        deadline = start_s + index * interval_s
        if deadline > end_s:
            return
        yield deadline
        index += 1


class SystemClock:
    """The real monotonic clock, which cancellation can interrupt."""

    def monotonic(self) -> float:
        """Return the current monotonic reading."""
        return time.monotonic()

    def wait_until(self, deadline_s: float, cancelled: Event) -> bool:
        """Sleep until the deadline; return False if cancelled first."""
        return not cancelled.wait(max(0.0, deadline_s - self.monotonic()))


class Observer:
    """Own sampling until stop joins it; the caller owns the artifact writer.

    Probe implementations must bound their own I/O. A stop timeout is an error,
    not permission to tear down a platform while its observer still runs.
    """

    def __init__(
        self,
        probe: ProcessProbe,
        clock: Clock,
        writer: ArtifactWriter,
        interval_s: float,
        *,
        startup_timeout_s: float = 5.0,
        budget: Callable[[], object] | None = None,
    ):
        """Bind the probe, clock and writer this observer samples through.

        ``startup_timeout_s`` must cover one sequential sweep of every role,
        and ``budget``, when given, is checked once per sweep so a run that
        would outgrow its artifact budget stops while its evidence is valid.
        """
        _positive(interval_s)
        _positive(startup_timeout_s)
        if budget is not None and not callable(budget):
            raise ValueError("budget guard must be callable")
        self._budget = budget
        # Workflow wiring must budget the first sequential sweep of all roles.
        self._startup_timeout = startup_timeout_s
        self._probe = probe
        self._clock = clock
        self._writer = writer
        self._interval = interval_s
        self._cancelled = Event()
        self._ready = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._phase: Phase = "preflight"
        self._error: BaseException | None = None
        self._stopped = False

    def start(self, phase: Phase) -> None:
        """Begin sampling, returning only once the first sweep has landed."""
        if phase not in _PHASES:
            raise ValueError("invalid observation phase")
        with self._lock:
            if self._thread is not None or self._stopped:
                raise RuntimeError("observer cannot be restarted")
            self._phase = phase
            self._thread = Thread(target=self._run, name="soak-observer", daemon=True)
            self._thread.start()
        if not self._ready.wait(self._startup_timeout):
            self._cancelled.set()
            raise TimeoutError("observer first sample exceeded startup timeout")

    def set_phase(self, phase: Phase) -> None:
        """Record a phase transition and label subsequent samples with it."""
        if phase not in _PHASES:
            raise ValueError("invalid observation phase")
        with self._lock:
            if self._thread is None or self._stopped or not self._thread.is_alive():
                raise RuntimeError("observer is not running")
            self._writer.append(
                "events",
                {
                    "schema": "nanolab-soak-v1",
                    "kind": "phase_transition",
                    "phase": phase,
                    "at_s": self._clock.monotonic(),
                },
            )
            self._phase = phase

    def stop(self, timeout_s: float) -> None:
        """Cancel sampling and join it, raising whatever the thread recorded."""
        _positive(timeout_s)
        with self._lock:
            self._stopped = True
            self._cancelled.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                raise TimeoutError("observer did not stop; probe still active")
        if self._error is not None:
            # Name the cause: this is often read from a workflow summary line
            # hours into a run, where the chained traceback is not shown.
            raise RuntimeError(
                "observer failed; partial evidence preserved: "
                f"{type(self._error).__name__}: {self._error}"
            ) from self._error

    def failure(self) -> BaseException | None:
        """Return the cause the sampling thread recorded, if it failed."""
        return self._error

    def _run(self) -> None:
        origin = self._clock.monotonic()
        index = 0
        try:
            while self._clock.wait_until(
                origin + index * self._interval, self._cancelled
            ):
                scheduled = origin + index * self._interval
                with self._lock:
                    phase = self._phase
                targets = self._probe.targets()
                if not targets or len(targets) > 128:
                    raise ValueError("missing targets or target limit exceeded")
                for target in targets:
                    samples = self._probe.sample(target, phase, scheduled)
                    if not samples or len(samples) > 10000:
                        raise ValueError("missing samples or sample limit exceeded")
                    for sample in samples:
                        if (
                            sample.target != target
                            or sample.phase != phase
                            or sample.scheduled_s != scheduled
                        ):
                            raise ValueError(
                                "probe returned mismatched sample identity"
                            )
                        self._writer.append(
                            "samples", {"schema": "nanolab-soak-v1", **asdict(sample)}
                        )
                if self._budget is not None:
                    self._budget()
                self._ready.set()
                index += 1
                now = self._clock.monotonic()
                next_index = max(index, math.ceil((now - origin) / self._interval))
                if next_index > index:
                    self._writer.append(
                        "events",
                        {
                            "schema": "nanolab-soak-v1",
                            "kind": "observation_gap",
                            "phase": phase,
                            "reason": "missed sampling deadlines",
                            "first_missed_s": origin + index * self._interval,
                            "next_scheduled_s": origin + next_index * self._interval,
                            "missed_count": next_index - index,
                        },
                    )
                index = next_index
        except BaseException as error:
            self._error = error
            self._cancelled.set()
        finally:
            self._ready.set()
