"""Shared measurement readers and local counter classification policy."""

from sonata_tasks.k6 import k6_value as k6_value
from sonata_tasks.k6 import k6_values as k6_values
from sonata_tasks.metrics import counter_delta as counter_delta
from sonata_tasks.metrics import finite_number as finite_number
from sonata_tasks.metrics import point_stats as point_stats


def is_counter(name: str, metric_type: str | None = None) -> bool:
    """Use explicit publisher type before Prometheus naming conventions."""
    if metric_type is not None:
        return metric_type == "counter"
    metric = name.split("@", 1)[0].split("{", 1)[0].strip()
    return (
        metric.endswith(("_total", "_count", "_sum")) or metric == "function_dispatch"
    )
