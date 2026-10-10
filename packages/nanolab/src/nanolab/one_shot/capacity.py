"""Conservative sustained capacity selection from complete raw trial groups."""

from __future__ import annotations

import math
from collections import defaultdict


def select_capacity(
    trials: list[dict], *, node: str, replicas: int, tolerance: float
) -> float:
    """Choose a tested rate only when all three 60-second trials sustain it."""
    groups = defaultdict(list)
    for trial in trials:
        if trial["node"] == node and trial["replicas"] == replicas:
            groups[trial["rate"]].append(trial)
    supported = []
    for rate, rows in groups.items():
        if (
            not math.isfinite(rate)
            or rate <= 0
            or len(rows) != 3
            or {row["repetition"] for row in rows} != {0, 1, 2}
        ):
            continue
        if all(
            row["warmupSeconds"] >= 30
            and row["measurementSeconds"] >= 60
            and row["planned"] > 0
            and row["emitted"] == row["planned"]
            and row["physicalCompletions"] / row["planned"] >= 0.99
            and row["generatorDeficit"] == row["censored"] == 0
            and row["validOutput"]
            and row["relativeServiceDrift"] <= tolerance
            for row in rows
        ):
            supported.append(rate)
    if not supported:
        raise ValueError(f"NOT_QUALIFIED: no sustained capacity for {node}/{replicas}")
    return max(supported)
