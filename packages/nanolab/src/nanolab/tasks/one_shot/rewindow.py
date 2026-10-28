"""Offline rewindowing of an immutable continuous arrival calendar."""

from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal
from math import isfinite


def rewindow_profile(
    source: dict,
    *,
    period_seconds: int,
    integral: Callable[[dict, float, float], float],
) -> tuple[dict, list[dict[str, dict[str, float]]]]:
    """Integrate physical seconds and regroup existing offsets, without resampling."""
    if (
        type(period_seconds) is not int
        or period_seconds <= 0
        or source.get("schemaVersion") != 1
    ):
        raise ValueError("expected a v1 calendar and positive integer period")
    source_period, epochs = source["periodSeconds"], source["epochs"]
    duration = source_period * epochs
    if duration % period_seconds or source_period % period_seconds:
        raise ValueError("target period must divide the source period and duration")
    curves = source["measurementCurves"]
    scope = {tuple(key.split("/", 1)) for key in curves}
    if not scope or any(len(key) != 2 or not all(key) for key in scope):
        raise ValueError("invalid curve scope")
    expected = {
        (phase, epoch, node, function)
        for node, function in scope
        for phase, indices in (("campaign", range(epochs)), ("warmup", [-1]))
        for epoch in indices
    }
    seen = set()
    grouped = defaultdict(list)
    warm = []
    for cell in source["cells"]:
        key = cell["phase"], cell["epoch"], cell["origin"], cell["function"]
        if type(cell["epoch"]) is not int or key not in expected or key in seen:
            raise ValueError("incomplete, duplicate or extra calendar scope")
        seen.add(key)
        if cell["phase"] == "warmup":
            start, stop = -source["warmupSeconds"], 0
        else:
            start, stop = (
                cell["epoch"] * source_period,
                (cell["epoch"] + 1) * source_period,
            )
        previous = None
        for offset in cell["offsetsSeconds"]:
            if (
                isinstance(offset, bool)
                or not isinstance(offset, (int, float))
                or not isfinite(offset)
                or not start <= offset < stop
                or (previous is not None and offset <= previous)
            ):
                raise ValueError("invalid source calendar offset")
            previous = offset
            if cell["phase"] == "campaign":
                grouped[
                    int(offset // period_seconds), cell["origin"], cell["function"]
                ].append(offset)
        if cell["phase"] == "warmup":
            warm.append(deepcopy(cell))
    if seen != expected:
        raise ValueError("incomplete source calendar scope")
    means, cells = [], []
    for epoch in range(int(duration // period_seconds)):
        window = {}
        for node, function in sorted(scope):
            start, stop = epoch * period_seconds, (epoch + 1) * period_seconds
            mean = integral(curves[node + "/" + function], start, stop) / period_seconds
            if not isfinite(mean) or mean < 0:
                raise ValueError("invalid integrated mean")
            window.setdefault(node, {})[function] = mean
            cells.append(
                {
                    "phase": "campaign",
                    "epoch": epoch,
                    "origin": node,
                    "function": function,
                    "offsetsSeconds": grouped[epoch, node, function],
                }
            )
        means.append(window)
    document = deepcopy(source)
    document.update(
        schemaVersion=2,
        curveTimeScaleSeconds=source_period,
        periodSeconds=period_seconds,
        epochs=len(means),
        cells=cells + warm,
    )
    document.pop("verification", None)
    return document, means


def quantize_means(
    means: list[dict[str, dict[str, float]]], *, quantum: float, period_seconds: int
) -> tuple[list[dict[str, dict[str, float]]], list[dict]]:
    """Round nearest with ties upward; retain integrated means and signed errors."""
    if isinstance(quantum, bool) or not isfinite(quantum) or quantum <= 0:
        raise ValueError("positive finite quantum required")
    q = Decimal(str(quantum))
    windows, audit = [], []
    for epoch, window in enumerate(means):
        rates = {}
        for node, functions in window.items():
            rates[node] = {}
            for function, mean in functions.items():
                if isinstance(mean, bool) or not isfinite(mean) or mean < 0:
                    raise ValueError("nonnegative finite mean required")
                units = (Decimal(str(mean)) / q).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
                rate = float(units * q)
                rates[node][function] = rate
                audit.append(
                    {
                        "epoch": epoch,
                        "node": node,
                        "function": function,
                        "meanRps": mean,
                        "integratedRequests": mean * period_seconds,
                        "oracleRps": rate,
                        "quantizationErrorRps": rate - mean,
                    }
                )
        windows.append(rates)
    return windows, audit
