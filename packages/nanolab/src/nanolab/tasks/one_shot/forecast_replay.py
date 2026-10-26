"""Replay a run's arrivals through Python copies of the NanoFaaS forecasters.

The copies follow platform/modules/forecasting as merged in Nanofaas/nanofaas#259
(WindowedForecastSource, EwmaForecastSource, TrendForecastSource): complete
UTC-aligned windows only, EWMA over all of them, Holt and ARI over the latest
`history` and answering the duration-weighted mean of their per-window forecasts
over the queried interval. Each interval is queried at its start minus the lead,
as the auction does.
"""

import csv
import json
import math
from collections import Counter, deque
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from itertools import pairwise
from pathlib import Path

from nanolab.one_shot.models import CampaignManifest
from nanolab.tasks.one_shot.experiment import generator_rows
from nanolab.tasks.one_shot.forecast_errors import (
    COLUMNS,
    forecast_error_rows,
    summarize_forecast_errors,
    with_prediction,
)
from nanolab.tasks.one_shot.trace import TraceArtifact

MAX_STEPS = 4096
CAP_WINDOWS = 60
# Registration precedes the first arrival; NanoFaaS starts at the next window.
REGISTRATION_LEAD_SECONDS = 60


@dataclass(frozen=True)
class Forecaster:
    """One NanoFaaS provider and its parameters, defaults as NanoFaaS's."""

    provider: str
    window: int
    alpha: float = 0.5
    level: float = 0.4
    trend: float = 0.4
    damping: float = 0.95
    order: int = 6
    history: int = 360

    def __post_init__(self):
        """Refuse what NanoFaaS would refuse at startup."""
        unit = {
            "ewma": (self.alpha,),
            "holt": (self.level, self.trend, self.damping),
            "ari": (),
        }.get(self.provider, ())
        if (
            self.provider not in ("ewma", "holt", "ari")
            or self.window < 1
            or not all(0 < value <= 1 for value in unit)
            or (self.provider != "ewma" and not 4 <= self.history <= 100_000)
            or (
                self.provider == "ari"
                and (not 1 <= self.order <= 64 or 2 * self.order + 2 > self.history)
            )
        ):
            raise ValueError(f"invalid forecaster: {self}")

    @property
    def label(self) -> str:
        """Provider and the parameters it uses, e.g. ari(window=10,order=6,...)."""
        used = {
            "ewma": ("window", "alpha"),
            "holt": ("window", "level", "trend", "damping", "history"),
            "ari": ("window", "order", "history"),
        }[self.provider]
        return (
            f"{self.provider}("
            + ",".join(f"{name}={getattr(self, name)}" for name in used)
            + ")"
        )


def parse_forecaster(spec: str) -> Forecaster:
    """Read ``provider:key=value,...``, e.g. ``holt:trend=0.2``; window 10 if unset."""
    provider, _, options = spec.partition(":")
    types = {field.name: field.type for field in fields(Forecaster)}
    values: dict = {"window": 10}
    for option in filter(None, options.split(",")):
        key, _, value = option.partition("=")
        if key not in types or key == "provider":
            raise ValueError(f"unknown forecaster option: {key}")
        values[key] = int(value) if types[key] in (int, "int") else float(value)
    return Forecaster(provider, **values)


def holt_path(rates, steps, level_weight, trend_weight, damping):
    """Holt's damped trend from the oldest retained window, ``steps`` ahead."""
    level, slope = rates[0], 0.0
    for rate in rates[1:]:
        previous = level
        level = level_weight * rate + (1 - level_weight) * (level + damping * slope)
        slope = trend_weight * (level - previous) + (1 - trend_weight) * damping * slope
    path, damped, factor = [], 0.0, 1.0
    for _ in range(steps):
        factor *= damping
        damped += factor
        path.append(max(0.0, level + damped * slope))
    return path


def _solve(system):
    """Gaussian elimination with partial pivoting, as TrendForecastSource."""
    k = len(system)
    for col in range(k):
        pivot = max(range(col, k), key=lambda r: abs(system[r][col]))
        system[col], system[pivot] = system[pivot], system[col]
        if system[col][col] == 0:
            return None
        for r in range(col + 1, k):
            factor = system[r][col] / system[col][col]
            for c in range(col, k + 1):
                system[r][c] -= factor * system[col][c]
    solution = [0.0] * k
    for r in range(k - 1, -1, -1):
        total = system[r][k] - sum(system[r][c] * solution[c] for c in range(r + 1, k))
        solution[r] = total / system[r][r]
    return solution


def ari_fit(rates, order):
    """Ridged least squares of each change on an intercept and its predecessors."""
    k = order + 1
    changes = [b - a for a, b in pairwise(rates)]
    system = [[0.0] * (k + 1) for _ in range(k)]
    for t in range(order, len(changes)):
        row = [1.0] + [changes[t - j] for j in range(1, order + 1)]
        for r in range(k):
            for c in range(k):
                system[r][c] += row[r] * row[c]
            system[r][k] += row[r] * changes[t]
    largest = max(system[r][r] for r in range(k))
    for r in range(k):
        system[r][r] += 1e-9 * (1 + largest)
    coef = _solve(system)
    return coef if coef and all(math.isfinite(v) for v in coef) else None


def ari_path(rates, steps, order):
    """Iterate the fitted changes forward, within twice the recent peak."""
    n, level = len(rates), rates[-1]
    coef = ari_fit(rates, order) if n >= 2 * order + 2 else None
    if coef is None:
        return [level] * steps
    cap = 2 * max(rates[max(0, n - CAP_WINDOWS) :])
    lags = [rates[n - 1 - j] - rates[n - 2 - j] for j in range(order)]
    path = []
    for _ in range(steps):
        change = coef[0] + sum(c * lag for c, lag in zip(coef[1:], lags, strict=True))
        following = min(cap, max(0.0, level + change))
        lags = [following - level, *lags[:-1]]
        level = following
        path.append(level)
    return path


def interval_mean(path, window, lo, hi):
    """Overlap-weighted mean; step 1 also covers before it, the last after it."""
    total = 0.0
    for step, value in enumerate(path):
        a = lo if step == 0 else max(lo, step * window)
        b = hi if step == len(path) - 1 else min(hi, (step + 1) * window)
        if b > a:
            total += (b - a) * value
    return total / (hi - lo)


def series_forecasts(times, model, *, starts, period, lead, registered):
    """One series' forecast for each interval start, or None before any window."""
    if registered is None:
        return [None] * len(starts)
    w = model.window
    first = math.floor(registered / w) + (registered % w != 0)
    counts = Counter(k for k in (math.floor(t / w) for t in times) if k >= first)
    following, mean = first, None
    retained = deque(maxlen=model.history if model.provider != "ewma" else 0)
    out = []
    for start in starts:
        completed = math.floor((start - lead) / w)
        while following < completed:
            rate = counts.get(following, 0) / w
            alpha = model.alpha if model.provider == "ewma" else 1.0
            mean = rate if mean is None else alpha * rate + (1 - alpha) * mean
            retained.append(rate)
            following += 1
        if mean is None:
            out.append(None)
        elif model.provider == "ewma":
            out.append(mean)
        else:
            lo, hi = start - following * w, start + period - following * w
            steps = int(min(MAX_STEPS, max(1, math.ceil(hi / w))))
            rates = list(retained)
            path = (
                holt_path(rates, steps, model.level, model.trend, model.damping)
                if model.provider == "holt"
                else ari_path(rates, steps, model.order)
            )
            out.append(interval_mean(path, w, lo, hi))
    return out


def replay_rows(manifest, trace, *, schedule, events, forecasters, lead):
    """Build the forecast-errors rows of every interval, edge and function per model."""
    if not forecasters:
        raise ValueError("at least one forecaster is required")
    labels = [model.label for model in forecasters]
    if len(labels) != len(set(labels)):
        raise ValueError("duplicate forecaster labels")
    base = forecast_error_rows(
        manifest, trace, schedule=schedule, events=events, epochs=[]
    )
    arrivals: dict[tuple[str, str], list[float]] = {}
    seen = set()
    for row in events:
        if row["event"] == "emitted" and row["originalId"] not in seen:
            seen.add(row["originalId"])
            key = (row["origin"], row["function"])
            arrivals.setdefault(key, []).append(row["emittedAt"])
    registered = min((t for times in arrivals.values() for t in times), default=None)
    if registered is not None:
        registered -= REGISTRATION_LEAD_SECONDS
    anchor = datetime.fromisoformat(manifest.anchor).timestamp()
    period = trace.period_seconds
    starts = [anchor + epoch * period for epoch in range(len(trace.windows))]
    rows = []
    for model in forecasters:
        predicted = {
            key: series_forecasts(
                sorted(arrivals.get(key, [])),
                model,
                starts=starts,
                period=period,
                lead=lead,
                registered=registered,
            )
            for key in {(row["node"], row["function"]) for row in base}
        }
        for row in base:
            rate = predicted[row["node"], row["function"]][row["epoch"]]
            replayed = {"model": model.label, **row, "forecastSource": "replay"}
            if rate is None:
                replayed["forecastSource"] = "no-window"
            else:
                with_prediction(replayed, rate, trace.flow_quantum)
            rows.append(replayed)
    return rows


def record_forecast_replay(run_dir: Path, output: Path, *, forecasters, lead=None):
    """Write forecast-replay.csv/json for a run; never overwrite them."""
    manifest = CampaignManifest.model_validate_json(
        (run_dir / "manifest.json").read_bytes()
    )
    trace = TraceArtifact.model_validate_json((run_dir / "trace.json").read_bytes())
    lead = manifest.lead_seconds if lead is None else lead
    rows = replay_rows(
        manifest,
        trace,
        schedule=json.loads((run_dir / "schedule.json").read_text()),
        events=generator_rows(run_dir / "generator.jsonl"),
        forecasters=forecasters,
        lead=lead,
    )
    models = {}
    for model in forecasters:
        summary = summarize_forecast_errors(
            [row for row in rows if row["model"] == model.label],
            manifest=manifest,
            trace=trace,
        )
        summary["forecastProvider"] = model.provider
        models[model.label] = {"forecaster": asdict(model), **summary}
    summary = {"schemaVersion": 1, "leadSeconds": lead, "models": models}
    with (output / "forecast-replay.csv").open("x", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=("model", *COLUMNS), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
    with (output / "forecast-replay.json").open("x") as stream:
        json.dump(summary, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return summary
