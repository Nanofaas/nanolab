#!/usr/bin/env python3
"""Compare the evidence shape of two one-shot runs: REFERENCE_RUN NEW_RUN.

Exit 0 when the runs have the same shape; otherwise print every difference and
exit 1. Each facet is a set of names gathered from every matching file under
the run directory (nested campaign and capacity runs included):

- metrics series / metrics labels: the Prometheus series names and label keys
  the reports read. `report.py` reads them only from `metrics.jsonl`
  (`metricSamples`: the `metrics` names, `nanofaas_runtime_active_handlers`
  among them, keyed by `node`/`function`/`replica`); `campaign.py` reads the
  per-run report rows, not Prometheus. The workbench node_exporter/cAdvisor
  series reach the reports only through the vm-resources rows below.
- Loki event kinds: `attributes.event` of `logging/*/queue-events.jsonl` and
  the `logger` of each line in `logging/*/loki-query.json`.
- vm-resources row keys: top-level keys of `vm-resources/*.jsonl` rows, and of
  their `containers[]`, `containerInventory[]` and `sampler` entries. Keys only
  the removed VM sampler wrote (raw counters, its monotonic clock and the
  correlation it embedded; no reader reads them from a Prometheus-policy run)
  are left out: `SAMPLER_ONLY`.
- coverage keys: top-level and per-node keys of `vm-resources/coverage.json`
  (`policy` is not descended into: it records the collector by design).
- supervisor fault kinds: the `kind` of failure rows in `supervision.jsonl`,
  the run's supervision event log.

Values are only checked for plausibility: every vm-resources node file is
non-empty, its median `memoryUsedBytes` is within 10x of the reference's, and
`metrics.jsonl` has rows. Finally every report of NEW_RUN (a `manifest.json`
with a `report.json` beside it) is regenerated in a temporary copy; that step
alone imports nanolab, so run the script with `uv run python`.
"""

from __future__ import annotations

import json
import shutil
import statistics
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

SAMPLER_ONLY = frozenset(
    {
        "monotonicSeconds",
        "bootId",
        "cpuCountersTicks",
        "clockTicksPerSecond",
        "elapsedSeconds",
        "runId",
        "mode",
        "functions",
        "phase",
        "epoch",
        "timeFromAnchorSeconds",
        "epochCandidates",
        "phaseCandidates",
        "correlationMethod",
        "correlationError",
        "correlationBoundsSeconds",
        "containers[].cpuUsageUsec",
        "containers[].nrPeriods",
        "containers[].nrThrottled",
        "containers[].throttledUsec",
        "containers[].memoryLimitBytes",
        "containers[].memoryPeakBytes",
        "containers[].swapCurrentBytes",
        "containers[].swapLimitBytes",
        "containers[].memoryEvents",
        "containers[].sampleMonotonic",
        "sampler.cpuSeconds",
        "sampler.collectionSeconds",
    }
)


def _rows(path: Path) -> Iterator[dict]:
    """Yield the JSON objects of a JSONL file, skipping partial lines."""
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def _vm_files(run: Path) -> list[Path]:
    return [
        path for path in sorted(run.rglob("vm-resources/*.jsonl")) if path.is_file()
    ]


def _row_keys(row: dict) -> set[str]:
    keys = set(row)
    for field in ("containers", "containerInventory"):
        for entry in row.get(field) or []:
            if isinstance(entry, dict):
                keys |= {f"{field}[].{key}" for key in entry}
    if isinstance(row.get("sampler"), dict):
        keys |= {f"sampler.{key}" for key in row["sampler"]}
    return keys


def _loki_kinds(run: Path) -> set[str]:
    kinds = set()
    for path in run.rglob("logging/*/queue-events.jsonl"):
        kinds |= {
            str(row["attributes"]["event"])
            for row in _rows(path)
            if isinstance(row.get("attributes"), dict) and "event" in row["attributes"]
        }
    for path in run.rglob("logging/*/loki-query.json"):
        try:
            streams = json.loads(path.read_text())["data"]["result"]
        except (ValueError, KeyError, TypeError):
            continue
        for stream in streams:
            for _stamp, line in stream.get("values", []):
                try:
                    logger = json.loads(line).get("logger")
                except (ValueError, AttributeError):
                    continue
                if logger:
                    kinds.add(f"logger:{logger}")
    return kinds


def facets(run: Path) -> dict[str, set[str]]:
    """Return each shape facet of `run` as a set of names."""
    series, labels, rows, coverage, faults = set(), set(), set(), set(), set()
    for path in run.rglob("metrics.jsonl"):
        for row in _rows(path):
            series |= set(row.get("metrics") or {})
            labels |= set(row) - {"metrics"}
    for path in _vm_files(run):
        for row in _rows(path):
            rows |= _row_keys(row) - SAMPLER_ONLY
    for path in run.rglob("vm-resources/coverage.json"):
        value = json.loads(path.read_text())
        coverage |= set(value)
        for node in (value.get("nodes") or {}).values():
            coverage |= {f"nodes.*.{key}" for key in node}
    for path in run.rglob("supervision.jsonl"):
        faults |= {
            str(row["kind"]) for row in _rows(path) if "fail" in str(row.get("kind"))
        }
    return {
        "metrics series": series,
        "metrics labels": labels,
        "Loki event kinds": _loki_kinds(run),
        "vm-resources row keys": rows,
        "coverage keys": coverage,
        "supervisor fault kinds": faults,
    }


def _medians(run: Path) -> dict[str, float | None]:
    """Median memoryUsedBytes per node file (None when the file has no rows)."""
    found: dict[str, list[float]] = {}
    for path in _vm_files(run):
        values = found.setdefault(path.stem, [])
        values += [
            row["memoryUsedBytes"]
            for row in _rows(path)
            if isinstance(row.get("memoryUsedBytes"), (int, float))
        ]
    return {node: statistics.median(v) if v else None for node, v in found.items()}


def compare(reference: Path, new: Path) -> list[str]:
    """Return every shape difference of `new` against `reference`."""
    differences = []
    old, now = facets(reference), facets(new)
    for name, expected in old.items():
        missing, extra = expected - now[name], now[name] - expected
        if missing:
            differences.append(f"{name}: missing in NEW: {', '.join(sorted(missing))}")
        if extra:
            differences.append(f"{name}: extra in NEW: {', '.join(sorted(extra))}")
    before, after = _medians(reference), _medians(new)
    for node, value in sorted(after.items()):
        if value is None:
            differences.append(f"vm-resources {node}: empty in NEW")
            continue
        base = before.get(node)
        if base and not (base / 10 <= value <= base * 10):
            differences.append(
                f"vm-resources {node} median memoryUsedBytes: {base:g} vs {value:g}: "
                "not the same order of magnitude"
            )
    if not any(any(_rows(path)) for path in new.rglob("metrics.jsonl")):
        differences.append("metrics.jsonl: empty in NEW")
    return differences


def regenerate(run: Path) -> list[str]:
    """Regenerate every report of `run` in a temporary copy; return failures."""
    from nanolab.one_shot.models import CampaignManifest
    from nanolab.one_shot.report import render_report
    from nanolab.tasks.one_shot.conservation import RunEvidence

    failures = []
    for manifest in sorted(run.rglob("manifest.json")):
        if not (manifest.parent / "report.json").exists():
            continue
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / "run"
            shutil.copytree(manifest.parent, copy)
            try:
                evidence = copy / "evidence.json"
                render_report(
                    CampaignManifest.model_validate_json(
                        (copy / "manifest.json").read_bytes()
                    ),
                    RunEvidence.model_validate_json(evidence.read_bytes())
                    if evidence.exists()
                    else None,
                    copy / "report.html",
                )
            except Exception as error:  # noqa: BLE001 - any failure is the finding
                where = manifest.parent.relative_to(run)
                failures.append(f"report.json does not regenerate in {where}: {error}")
    return failures


def main(argv: list[str]) -> int:
    """Print every difference; exit 0 only when there is none."""
    if len(argv) != 2:
        print(__doc__.splitlines()[0], file=sys.stderr)
        return 2
    reference, new = (Path(arg) for arg in argv)
    differences = compare(reference, new) + regenerate(new)
    for line in differences:
        print(line)
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
