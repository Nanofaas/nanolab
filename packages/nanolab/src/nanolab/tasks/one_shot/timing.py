"""Select period and readiness lead from measured parallel wall times."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from nanolab.one_shot.models import Purpose, TimingQualification
from nanolab.tasks.one_shot.statistics import quantile as empirical_quantile


@dataclass(frozen=True)
class WallTimingSample:
    """A complete operation or explicitly censored observation, never CPU sums."""

    auction_seconds: float | None
    ready_seconds: float | None
    censored: bool = False
    node_cpu_seconds: list[float] = field(default_factory=list)


def select_timing(
    samples: list[WallTimingSample],
    *,
    period_candidates: list[float],
    max_trace_resolution: float,
    minimum_samples: int,
    quantile: float,
    margin_seconds: float,
    ready_margin_seconds: float,
    epsilon: float,
    protocol_budget_seconds: float,
    provider: str,
    purpose: Purpose,
    fingerprint: str,
    profile_sha256: str,
) -> TimingQualification:
    """Require zero censoring and complete sample coverage before selecting a T."""
    if not samples or not period_candidates:
        raise ValueError("timing requires observations and explicit period candidates")
    for sample in samples:
        for value in (sample.auction_seconds, sample.ready_seconds):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("wall timing must be finite and nonnegative")
    censored = sum(
        sample.censored
        or sample.auction_seconds is None
        or sample.ready_seconds is None
        for sample in samples
    )
    durations = [sample.auction_seconds for sample in samples]
    ready = [sample.ready_seconds for sample in samples]
    complete_durations = [value for value in durations if value is not None]
    complete_ready = [value for value in ready if value is not None]
    auction_quantile = (
        empirical_quantile(complete_durations, quantile) if complete_durations else 0
    )
    lead = (
        max(
            empirical_quantile(complete_ready, quantile) if complete_ready else 0,
            protocol_budget_seconds,
        )
        + ready_margin_seconds
    )
    candidates = sorted(set(period_candidates))
    eligible = [
        period
        for period in candidates
        if period <= max_trace_resolution
        and lead < period
        and max(auction_quantile + margin_seconds, protocol_budget_seconds)
        <= epsilon * period
    ]
    qualified = not censored and len(samples) >= minimum_samples and bool(eligible)
    return TimingQualification(
        provider=provider,
        purpose=purpose,
        environment_fingerprint=fingerprint,
        profile_sha256=profile_sha256,
        qualified=qualified,
        period_seconds=eligible[0] if eligible else candidates[-1],
        lead_seconds=lead,
        quantile=quantile,
        quantile_seconds=auction_quantile,
        margin_seconds=margin_seconds,
        epsilon=epsilon,
        sample_count=len(samples),
        censored_count=censored,
        samples_seconds=durations,
        minimum_samples=minimum_samples,
        period_candidates=candidates,
        max_trace_resolution=max_trace_resolution,
        ready_samples_seconds=ready,
        reason=None
        if qualified
        else "NOT_QUALIFIED: incomplete, censored or no valid period",
    )
