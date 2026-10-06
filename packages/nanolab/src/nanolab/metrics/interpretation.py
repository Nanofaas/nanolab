"""Pure interpretation of k6 exports and labelled Prometheus observations."""

import math
from collections.abc import Mapping
from itertools import pairwise
from typing import Any


def finite_number(value: object, *, nonnegative: bool = False) -> float:
    """Read an observed numeric value without treating booleans as measurements."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("measurement must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError("measurement must be a finite number") from error
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise ValueError("measurement must be a finite nonnegative number")
    return number


def k6_values(metrics: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Read summary-export's flat values or handleSummary's nested values."""
    entry = metrics.get(name, {})
    if not isinstance(entry, Mapping):
        raise ValueError(f"k6.{name} must be a mapping")
    values = entry.get("values", entry)
    if not isinstance(values, Mapping):
        raise ValueError(f"k6.{name} must be a mapping")
    return values


def k6_value(values: Mapping[str, Any], name: str, *keys: str) -> float:
    """Require a finite nonnegative k6 value; first present alias wins."""
    for key in keys:
        if key not in values:
            continue
        value = values[key]
        try:
            # k6 writes JSON numbers. Numeric strings indicate malformed evidence.
            if not isinstance(value, (int, float)):
                raise ValueError("k6 measurement must be numeric")
            number = finite_number(value, nonnegative=True)
            if (
                name in {"http_req_failed", "checks"}
                and key in {"rate", "value"}
                and number > 1
            ):
                raise ValueError("rate must be between 0 and 1")
            return number
        except ValueError as error:
            raise ValueError(
                f"invalid required k6 metric {name}.{key}: {error}"
            ) from error
    raise ValueError(f"missing required k6 metric {name}: {'/'.join(keys)}")


def is_counter(name: str, metric_type: str | None = None) -> bool:
    """Use explicit publisher type before Prometheus naming conventions."""
    if metric_type is not None:
        return metric_type == "counter"
    metric = name.split("@", 1)[0].split("{", 1)[0].strip()
    return (
        metric.endswith(("_total", "_count", "_sum")) or metric == "function_dispatch"
    )


def counter_delta(points: list[dict[str, Any]]) -> float | None:
    """Count increments/resets per publisher; absent or invalid data is unavailable."""
    if not points:
        return None
    series: dict[tuple, list[dict[str, Any]]] = {}
    try:
        for point in points:
            finite_number(point.get("value"), nonnegative=True)
            key = tuple(sorted(point.get("labels", {}).items()))
            series.setdefault(key, []).append(point)
    except ValueError:
        return None
    total = 0.0
    for samples in series.values():
        if len(samples) < 2:
            return None
        if all("timestamp" in point for point in samples):
            samples = sorted(samples, key=lambda point: point["timestamp"])
        values = [finite_number(point["value"]) for point in samples]
        for previous, current in pairwise(values):
            total += current - previous if current >= previous else current
    return total if math.isfinite(total) else None


def point_stats(points: list[dict], *, counter: bool = False) -> dict[str, float | int]:
    """Summarize ordered samples without converting unavailable evidence to zero."""
    merged: dict[str, float] = {}
    invalid = 0
    for index, point in enumerate(points):
        try:
            value = finite_number(point.get("value"), nonnegative=counter)
        except ValueError:
            invalid += 1
            continue
        timestamp = point.get("timestamp", str(index))
        merged[timestamp] = merged.get(timestamp, 0.0) + value
    if invalid:
        return {"points": len(merged), "invalid_points": invalid}
    values = (
        [merged[key] for key in sorted(merged)]
        if all("timestamp" in point for point in points)
        else list(merged.values())
    )
    if not values:
        return {"points": 0}
    result: dict[str, float | int] = {
        "points": len(values),
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
    }
    delta = counter_delta(points) if counter else values[-1] - values[0]
    if delta is not None:
        result["delta"] = delta
    return result
