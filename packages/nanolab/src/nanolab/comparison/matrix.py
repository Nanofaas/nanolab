"""The order in which the comparison's runs are executed, and where each lands.

The only interesting decision here is the order. Running all three repetitions of
one variant before moving to the next is the obvious layout and the wrong one: it
makes "which variant" and "when it ran" the same axis. An Azure host with a noisy
neighbour, a page cache that fills, a disk that slows as it fills — any drift over
the hour the matrix takes would land entirely on whichever variants ran late, and
the run would report it as a property of those builds.

Interleaving instead — every variant once, then every variant again — spreads each
build's three samples across the whole window, so a drift shows up as variance
within each variant rather than as a difference between them. The cost is eleven
extra control-plane redeploys, which is a helm upgrade and a pod restart apiece:
minutes against a run measured in hours, and it buys the difference between a
comparison and a coincidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nanolab.images.control_plane_variants import ControlPlaneVariant


@dataclass(frozen=True, slots=True)
class ComparisonCell:
    """One (variant, repetition) pair: a single load-test run."""

    variant: ControlPlaneVariant
    repetition: int

    def run_dir(self, root: Path) -> Path:
        """Return the directory this cell's run writes its artefacts to."""
        # Keyed by variant first so a half-finished matrix is still readable by
        # build, and by repetition second so the reader can see how many landed.
        return root / self.variant.key / f"run-{self.repetition}"

    @property
    def label(self) -> str:
        """Return the human-readable name this cell takes in the manifest."""
        return f"{self.variant.key} run {self.repetition}"


def build_matrix(
    variants: tuple[ControlPlaneVariant, ...], repetitions: int
) -> tuple[ComparisonCell, ...]:
    """Every cell, ordered repetition-major so the variants interleave."""
    if repetitions < 1:
        raise ValueError("a comparison needs at least one repetition")
    if not variants:
        raise ValueError("a comparison needs at least one variant")
    return tuple(
        ComparisonCell(variant=variant, repetition=repetition)
        for repetition in range(1, repetitions + 1)
        for variant in variants
    )


def completed(cell: ComparisonCell, root: Path) -> bool:
    """Whether this cell already produced the summary the report reads.

    A matrix runs for well over an hour, and every interruption so far has cost a
    full redo of cells whose results were already on disk and correct.

    Both files, not just the k6 summary. The summary was the original marker and
    it is written BEFORE the metrics snapshot, so a cell whose snapshot failed —
    a Prometheus query timing out over a tunnel, say — left a summary behind and
    would have been skipped for ever, silently absent from every figure that
    reads the snapshot. The report tolerates a missing cell; it cannot tolerate
    one that claims to be there.
    """
    run_dir = cell.run_dir(root)
    return (run_dir / "k6-summary.json").is_file() and (
        run_dir / "metrics" / "prometheus-snapshot.json"
    ).is_file()


def pending(
    cells: tuple[ComparisonCell, ...], root: Path
) -> tuple[ComparisonCell, ...]:
    """Return the cells that have not completed yet, in matrix order."""
    return tuple(cell for cell in cells if not completed(cell, root))
