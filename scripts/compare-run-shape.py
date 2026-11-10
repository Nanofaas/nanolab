#!/usr/bin/env python3
"""Compare the evidence shape of two one-shot runs: REFERENCE_RUN NEW_RUN.

Exit 0 when the runs have the same shape; otherwise print every difference and
exit 1. Each facet is compared within the same relative evidence file, including
nested campaign and capacity trials; evidence in another trial cannot fill a gap:

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
- supervisor fault kinds: the anomaly `kind`s in `workbench/supervisor-status.json`
  and the kind in each `operational.failure` cause of `supervision.jsonl` (the
  run's supervision event log; a one-shot run has no `events.jsonl`). A
  workbench cause reads `target: kind: detail`; any other cause (a run on main,
  without the supervisor) contributes its middle field the same way, or the
  whole cause when it has no such fields.

Values are only checked for plausibility: every vm-resources node file and
every coverage.json node of REFERENCE_RUN is present in NEW_RUN, every node file
is non-empty, its median `memoryUsedBytes` is within 10x of the reference's, and
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


def _loki_kinds(path: Path) -> set[str]:
    if path.name == "queue-events.jsonl":
        return {
            str(row["attributes"]["event"])
            for row in _rows(path)
            if isinstance(row.get("attributes"), dict) and "event" in row["attributes"]
        }
    kinds = set()
    try:
        streams = json.loads(path.read_text())["data"]["result"]
    except (ValueError, KeyError, TypeError):
        return kinds
    for stream in streams:
        for _stamp, line in stream.get("values", []):
            try:
                logger = json.loads(line).get("logger")
            except (ValueError, AttributeError):
                continue
            if logger:
                kinds.add(f"logger:{logger}")
    return kinds


def _fault_kind(cause: str) -> str:
    """Return `kind` from a `target: kind: detail` cause, else the whole cause."""
    parts = cause.split(": ", 2)
    return parts[1] if len(parts) == 3 else cause


def _coverage_nodes(run: Path) -> set[tuple[str, str]]:
    return {
        (str(path.relative_to(run)), node)
        for path in run.rglob("vm-resources/coverage.json")
        for node in json.loads(path.read_text()).get("nodes") or {}
    }


def facets(run: Path) -> dict[tuple[str, str], set[str]]:
    """Return shape facets keyed by relative evidence path and facet name."""
    found = {}
    for path in run.rglob("metrics.jsonl"):
        series, labels = set(), set()
        for row in _rows(path):
            series |= set(row.get("metrics") or {})
            labels |= set(row) - {"metrics"}
        relative = str(path.relative_to(run))
        found[relative, "metrics series"] = series
        found[relative, "metrics labels"] = labels
    for path in _vm_files(run):
        keys = set()
        for row in _rows(path):
            keys |= _row_keys(row) - SAMPLER_ONLY
        found[str(path.relative_to(run)), "vm-resources row keys"] = keys
    for path in run.rglob("vm-resources/coverage.json"):
        value = json.loads(path.read_text())
        keys = set(value)
        for node, data in (value.get("nodes") or {}).items():
            keys |= {f"nodes.{node}.{key}" for key in data}
        found[str(path.relative_to(run)), "coverage keys"] = keys
    for pattern in ("logging/*/queue-events.jsonl", "logging/*/loki-query.json"):
        for path in run.rglob(pattern):
            found[str(path.relative_to(run)), "Loki event kinds"] = _loki_kinds(path)
    for path in run.rglob("supervision.jsonl"):
        found[str(path.relative_to(run)), "supervisor fault kinds"] = {
            _fault_kind(str(row.get("cause")))
            for row in _rows(path)
            if row.get("kind") == "operational.failure"
        }
    for path in run.rglob("workbench/supervisor-status.json"):
        anomalies = json.loads(path.read_text()).get("anomalies") or []
        found[str(path.relative_to(run)), "supervisor fault kinds"] = {
            str(anomaly.get("kind")) for anomaly in anomalies
        }
    return found


def _medians(run: Path) -> dict[str, float | None]:
    """Median memoryUsedBytes per node file (None when the file has no rows)."""
    found: dict[str, list[float]] = {}
    for path in _vm_files(run):
        values = found.setdefault(str(path.relative_to(run)), [])
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
    old_paths = {path for path, _name in old}
    new_paths = {path for path, _name in now}
    for path in sorted(old_paths - new_paths):
        differences.append(f"{path}: file missing in NEW")
    for path in sorted(new_paths - old_paths):
        differences.append(f"{path}: extra file in NEW")
    for key in sorted(old.keys() & now.keys()):
        path, name = key
        missing, extra = old[key] - now[key], now[key] - old[key]
        if missing:
            differences.append(
                f"{path}: {name}: missing in NEW: {', '.join(sorted(missing))}"
            )
        if extra:
            differences.append(
                f"{path}: {name}: extra in NEW: {', '.join(sorted(extra))}"
            )
    before, after = _medians(reference), _medians(new)
    for path, node in sorted(_coverage_nodes(reference) - _coverage_nodes(new)):
        differences.append(f"{path} {node}: node missing in NEW")
    for path, value in sorted(after.items()):
        if value is None:
            differences.append(f"{path}: empty in NEW")
            continue
        base = before.get(path)
        if base and not (base / 10 <= value <= base * 10):
            differences.append(
                f"{path} median memoryUsedBytes: {base:g} vs {value:g}: "
                "not the same order of magnitude"
            )
    metrics = sorted(new.rglob("metrics.jsonl"))
    if not metrics:
        differences.append("metrics.jsonl: empty in NEW")
    for path in metrics:
        if not any(_rows(path)):
            differences.append(f"{path.relative_to(new)}: empty in NEW")
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
