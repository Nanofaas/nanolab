"""Backend-independent clocks, probes, diagnostics and workload ownership."""

from pathlib import Path
from threading import Event
from typing import Protocol

from nanolab.tasks.soak.models import Phase, Sample, Target


class Clock(Protocol):
    """Schedule absolute deadlines without hiding cancellation or scrape drift."""

    def monotonic(self) -> float:
        """Return the monotonic clock in seconds."""
        ...

    def wait_until(self, deadline_s: float, cancelled: Event) -> bool:
        """Return false if cancellation prevents reaching the deadline."""
        ...


class ProcessProbe(Protocol):
    """Observe each process independently, retaining failures as unavailable data."""

    def targets(self) -> tuple[Target, ...]:
        """Return current process identities for the selected application roles."""
        ...

    def sample(
        self, target: Target, phase: Phase, scheduled_s: float
    ) -> tuple[Sample, ...]:
        """Collect bounded observations for one scheduled checkpoint."""
        ...


class DiagnosticAdapter(Protocol):
    """Advertise real capabilities and preserve evidence of actual completion."""

    def capabilities(self, target: Target) -> frozenset[str]:
        """Return operations this adapter can perform against the target."""
        ...

    def capture(
        self, target: Target, checkpoint: str, output_dir: Path, timeout_s: float
    ) -> Path:
        """Write a diagnostic receipt, including failed or unverified operations."""
        ...


# The receipt a driver writes into the output directory it is handed. Part of
# the contract rather than an implementation detail: `run` persists this file
# before it raises, so a caller that only reads the return value loses the
# evidence of the load that failed — which is the one whose evidence is wanted.
WORKLOAD_RECEIPT = "workload-receipt.json"


class WorkloadDriver(Protocol):
    """Own the generator and reap only its children on completion or cancellation."""

    def run(self, output_dir: Path, duration_s: float, cancelled: Event) -> Path:
        """Run a bounded load phase and persist its workload receipt."""
        ...

    def stop(self, timeout_s: float) -> None:
        """Idempotently stop and reap owned children within the supplied timeout."""
        ...
