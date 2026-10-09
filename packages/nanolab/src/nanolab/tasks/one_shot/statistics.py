"""Reproducible physical-occupancy statistics with explicit stability criteria."""

from __future__ import annotations

import math
import random
import statistics
from typing import Any


def quantile(values: list[float], probability: float) -> float:
    """Compute the linearly interpolated empirical quantile (Hyndman-Fan type 7)."""
    if not values or not 0 <= probability <= 1:
        raise ValueError("quantile needs samples and a probability")
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def summarize_service(
    samples: list[dict[str, Any]],
    *,
    minimum_samples: int,
    relative_ci: float,
    confidence: float = 0.95,
    seed: int = 7,
    bootstrap_repetitions: int = 1000,
    quantiles: list[float] | None = None,
) -> dict[str, Any]:
    """Use physical occupancy only; never silently discard missing/censored rows."""
    if any(sample["state"] != "complete" for sample in samples):
        raise ValueError("incomplete physical occupancy samples")
    durations = [float(sample["occupancySeconds"]) for sample in samples]
    if any(not math.isfinite(value) or value <= 0 for value in durations):
        raise ValueError("physical occupancy must be finite and positive")
    if not durations or minimum_samples < 1 or not 0 < confidence < 1:
        raise ValueError("invalid service statistics parameters")
    mean = statistics.mean(durations)
    rng = random.Random(seed)
    means = [
        statistics.mean(rng.choices(durations, k=len(durations)))
        for _ in range(bootstrap_repetitions)
    ]
    lower = quantile(means, (1 - confidence) / 2)
    upper = quantile(means, (1 + confidence) / 2)
    half_width = max(mean - lower, upper - mean) / mean
    return {
        "sampleCount": len(durations),
        "meanSeconds": mean,
        "stddevSeconds": statistics.stdev(durations) if len(durations) > 1 else 0,
        "p95Seconds": quantile(durations, 0.95),
        "quantilesSeconds": {
            str(probability): quantile(durations, probability)
            for probability in (quantiles if quantiles is not None else [0.5, 0.95])
        },
        "confidence": confidence,
        "confidenceIntervalSeconds": [lower, upper],
        "relativeHalfWidth": half_width,
        "method": "seeded-percentile-bootstrap",
        "bootstrapRepetitions": bootstrap_repetitions,
        "seed": seed,
        "qualified": len(durations) >= minimum_samples and half_width <= relative_ci,
    }
