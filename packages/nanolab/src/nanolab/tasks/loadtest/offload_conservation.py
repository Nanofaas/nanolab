"""Pure reconciliation of k6 offload traffic against edge/cloud Prometheus counters.

No I/O: callers fetch the k6 summary JSON and the two raw Prometheus exposition
texts (e.g. via ``CapturePrometheusSnapshot``) and pass them in.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ConservationReport:
    """The verdict of the reconciliation and the counters behind it."""

    passed: bool
    failures: tuple[str, ...]
    numbers: dict[str, float]


def _k6_counter_value(
    k6_summary: Mapping[str, Any], name: str, tags: Mapping[str, str] | None = None
) -> float:
    """Read a k6 --summary-export counter's count.

    Accept flat summary-export fields and handleSummary's nested "values".
    k6 also
    only emits a per-tag submetric (key ``"name{tag:value}"``) for tag
    combinations referenced by a threshold; untagged custom counters are the
    reliable source for anything else.
    """
    metrics = k6_summary.get("metrics", {})
    if not isinstance(metrics, Mapping):
        raise ValueError(f"missing required k6 counter {name}")
    entry = None
    if tags:
        tag_str = ",".join(f"{key}:{value}" for key, value in tags.items())
        entry = metrics.get(f"{name}{{{tag_str}}}")
    if entry is None:
        entry = metrics.get(name)
    if not isinstance(entry, Mapping):
        raise ValueError(f"missing required k6 counter {name}")
    values = entry.get("values", entry)
    if not isinstance(values, Mapping) or "count" not in values:
        raise ValueError(f"missing required k6 counter {name}")
    return _finite_counter(values["count"], name)


def _finite_counter(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid counter {name}: {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"invalid counter {name}: {value!r}")
    return number


_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+(?P<value>\S+)(?:\s+\S+)?$"
)
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*("(?:[^"\\]|\\.)*")')


def _sum_metric(
    text: str, name: str, labels: Mapping[str, str], *, required: bool = False
) -> float:
    total = 0.0
    found = False
    for line in text.splitlines():
        sample = _SAMPLE.fullmatch(line.strip())
        if sample is None or sample["name"] != name:
            continue
        actual = {
            key: json.loads(value)
            for key, value in _LABEL.findall(sample["labels"] or "")
        }
        if all(actual.get(key) == value for key, value in labels.items()):
            total += _finite_counter(sample["value"], name)
            found = True
    if required and not found:
        raise ValueError(f"missing required counter {name} for {dict(labels)}")
    return _finite_counter(total, name)


def evaluate_conservation(
    *,
    k6_summary: Mapping[str, Any],
    edge_metrics: str,
    cloud_metrics: str,
    offloadable: str,
    control: str,
    tolerance: int = 5,
) -> ConservationReport:
    """Reconcile the k6 counters against the edge and cloud metric texts.

    Checks that the offloadable function's requests, the edge's offload counter
    and the cloud's successes agree, that the control function is never
    offloaded, and that no offload or retry failed. Every divergence is collected
    into the report rather than raised on the first, so one run names them all.
    """
    failures: list[str] = []
    numbers: dict[str, float] = {}

    def record(label: str, read: Callable[[], float]) -> float:
        try:
            value = read()
        except ValueError as exc:
            failures.append(str(exc))
            value = float("nan")
        numbers[label] = value
        return value

    def check_close(a_label: str, a: float, b_label: str, b: float) -> None:
        if abs(a - b) > tolerance:
            failures.append(
                f"{a_label} ({a}) diverges from {b_label} ({b}) "
                f"beyond tolerance {tolerance}"
            )

    # 1. k6 requests for the offloadable function vs edge function_success_total.
    k6_offloadable_reqs = record(
        "k6_offloadable_requests",
        lambda: _k6_counter_value(k6_summary, "offloadable_requests"),
    )
    edge_success_offloadable = record(
        "edge_function_success_offloadable",
        lambda: _sum_metric(
            edge_metrics,
            "function_success_total",
            {"function": offloadable},
            required=True,
        ),
    )
    check_close(
        "k6 requests for offloadable",
        k6_offloadable_reqs,
        "edge function_success_total for offloadable",
        edge_success_offloadable,
    )

    # 2. k6 offloaded_requests vs the edge's nanofaas_offload_total vs cloud success.
    k6_offloaded = record(
        "k6_offloaded_requests",
        lambda: _k6_counter_value(
            k6_summary, "offloaded_requests", {"function": offloadable}
        ),
    )
    if k6_offloaded == 0:
        failures.append("no requests were offloaded to the cloud")
    edge_offload_total = record(
        "edge_offload_total",
        lambda: _sum_metric(
            edge_metrics,
            "nanofaas_offload_total",
            {"function": offloadable},
            required=True,
        ),
    )
    cloud_success_offloadable = record(
        "cloud_function_success_offloadable",
        lambda: _sum_metric(
            cloud_metrics,
            "function_success_total",
            {"function": offloadable},
            required=True,
        ),
    )
    check_close(
        "k6 offloaded_requests",
        k6_offloaded,
        "edge nanofaas_offload_total (depth+est_wait)",
        edge_offload_total,
    )
    check_close(
        "edge nanofaas_offload_total (depth+est_wait)",
        edge_offload_total,
        "cloud function_success_total for offloadable",
        cloud_success_offloadable,
    )
    check_close(
        "k6 offloaded_requests",
        k6_offloaded,
        "cloud function_success_total for offloadable",
        cloud_success_offloadable,
    )
    # 3. the control function must never be offloaded, on either control plane.
    edge_offload_control = record(
        "edge_offload_control",
        lambda: _sum_metric(
            edge_metrics, "nanofaas_offload_total", {"function": control}
        ),
    )
    if edge_offload_control > tolerance:
        failures.append(
            f"edge nanofaas_offload_total for control function {control} "
            f"is {edge_offload_control}, expected 0"
        )
    if control in cloud_metrics:
        failures.append(
            f"cloud metrics mention the control function {control}; "
            "it must never run there"
        )

    # 4. no offload failures, no retries, on the edge.
    if "nanofaas_offload_failure_total" in edge_metrics:
        failures.append(
            "edge exposes nanofaas_offload_failure_total; offload calls must never fail"
        )
    for function in (offloadable, control):
        retries = record(
            f"edge_retries_{function}",
            lambda function=function: _sum_metric(
                edge_metrics, "function_retry_total", {"function": function}
            ),
        )
        if retries > tolerance:
            failures.append(
                f"edge function_retry_total for {function} is {retries}, expected 0"
            )

    return ConservationReport(
        passed=not failures, failures=tuple(failures), numbers=numbers
    )
