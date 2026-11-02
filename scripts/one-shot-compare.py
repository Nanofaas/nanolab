#!/usr/bin/env python3
"""Compare a one-shot observation run with a reference run of the same scenario.

Splits the comparison as the multi-site design does: edge-side behaviour must
match the reference within run-to-run noise; network latency and anything the
cloud serves may differ (different site, architecture or location) and is only
recorded. Reads each run directory's report.json and result.json; writes
comparison.json and comparison.md into --output.

Per-node work comes from nanoFaaS's own counters in queue-samples-<node>.jsonl,
over the campaign of schedule.json (the run directory, or --*-samples when a
run keeps them elsewhere). Those count every request; the report's per-edge
outcomes and routing count only the requests whose physical proof was
collected, so they are judged only when both runs proved nearly every success.
"""

import argparse
import json
import re
import statistics
from datetime import datetime
from pathlib import Path

# Relative difference beyond which a must-match quantity is flagged for review.
TOLERANCE = 0.10
# Proof-based quantities are judged only when both runs prove this share of
# successful responses; below it they compare two unequal samples.
PROOF_COVERAGE = 0.95
# Requests scheduled last are still running when the schedule ends.
DRAIN_SECONDS = 60
# nanoFaaS counters per node and function, summed over functions.
COUNTERS = {
    # Executions on the node's replicas, whoever sent them.
    "executions": "function_latency_ms_seconds_count",
    # Requests its gateway answered, those it forwarded included.
    "handled": "function_e2e_latency_ms_seconds_count",
    # Sync-queue admission refusals (estimated wait, depth, timeout).
    "rejected": "sync_queue_rejected_total",
}
SAMPLE_TIME = re.compile(r'"atUtc":\s*"([^"]+)"')


def load(run: Path) -> tuple[dict, dict]:
    """Read a run's report and terminal result."""
    return (
        json.loads((run / "report.json").read_text()),
        json.loads((run / "result.json").read_text()),
    )


def relative(reference: float, run: float) -> float | None:
    """Return the run's relative difference from the reference."""
    if reference == 0:
        return None if run == 0 else float("inf")
    return (run - reference) / reference


def row(name: str, reference, run, *, exact: bool = False) -> dict:
    """Compare one quantity; exact quantities must be equal."""
    if exact:
        ok = reference == run
        return {"quantity": name, "reference": reference, "run": run, "ok": ok}
    difference = relative(reference, run)
    ok = difference is not None and abs(difference) <= TOLERANCE
    return {
        "quantity": name,
        "reference": reference,
        "run": run,
        "relativeDifference": difference,
        "ok": ok or (reference == run == 0),
    }


def campaign_window(directory: Path) -> tuple[float, float]:
    """Return the scheduled campaign's span, extended by the drain allowance."""
    times = [
        row["scheduledAt"]
        for row in json.loads((directory / "schedule.json").read_text())
        if row["phase"] == "campaign"
    ]
    return min(times), max(times) + DRAIN_SECONDS


def sample_totals(line: str) -> dict[str, float]:
    """Sum one sample's counters over functions."""
    totals = dict.fromkeys(COUNTERS, 0.0)
    for sample in json.loads(line)["samples"]:
        for key, name in COUNTERS.items():
            if sample["name"] == name:
                totals[key] += sample["value"]
    return totals


def node_counters(directory: Path) -> dict[str, dict[str, int]]:
    """Count each node's work over the campaign from its cumulative counters.

    The baseline is the last sample at or before the campaign start (the
    first one inside it when sampling began late); only two lines per node are
    parsed, since each sample holds every histogram bucket. A failed sample
    (an unreachable control plane) has no counters and is skipped, so a node
    that stopped early counts up to its last sample.
    """
    start, end = campaign_window(directory)
    counters = {}
    for path in sorted(directory.glob("queue-samples-*.jsonl")):
        node = path.stem.removeprefix("queue-samples-")
        before = first = last = None
        with path.open() as stream:
            for line in stream:
                found = SAMPLE_TIME.search(line, 0, 200)
                if found is None or '"samples":' not in line:
                    continue
                at = datetime.fromisoformat(found.group(1)).timestamp()
                if at <= start:
                    before = line
                elif at <= end:
                    first = first or line
                    last = line
        baseline = before or first
        if baseline is None or last is None:
            raise SystemExit(f"{path}: no sample inside the campaign")
        opening, closing = sample_totals(baseline), sample_totals(last)
        counted = {key: int(closing[key] - opening[key]) for key in COUNTERS}
        if any(value < 0 for value in counted.values()):
            raise SystemExit(f"{path}: a counter went back (control plane restart)")
        counters[node] = counted
    if not counters:
        raise SystemExit(f"{directory}: no queue-samples-*.jsonl")
    return counters


def counter_rows(reference: dict, run: dict) -> tuple[list[dict], list[dict]]:
    """Split per-node counters into edge behaviour and recorded quantities."""
    judged, noted = [], []
    for node in sorted(reference.keys() | run.keys()):
        mine, theirs = run.get(node, {}), reference.get(node, {})
        for key in COUNTERS:
            item = row(f"{node} {key}", theirs.get(key, 0), mine.get(key, 0))
            # Admission refusals swing with a threshold; the cloud may differ.
            edge_work = node != "cloud" and key != "rejected"
            (judged if edge_work else noted).append(item)
    return judged, noted


def returned(report: dict) -> dict[str, int]:
    """Campaign responses as the generator received them, proofs aside."""
    return report["returnedResponses"]["phases"]["campaign"]["total"]


def proof_coverage(report: dict) -> float:
    """Share of successful responses whose physical completion was proven."""
    success = returned(report)["successResponse"]
    return report["completed"] / success if success else 0.0


def shares(counts: dict[str, int]) -> dict[str, float]:
    """Normalize counts to shares of their total."""
    total = sum(counts.values())
    return {key: value / total for key, value in counts.items()} if total else {}


def must_match(reference: dict, run: dict, ref_result: dict, run_result: dict):
    """Edge-side quantities expected to agree within run-to-run noise."""
    theirs, mine = returned(reference), returned(run)
    rows = [
        row("planned originals", reference["planned"], run["planned"], exact=True),
        row("emitted originals", reference["emitted"], run["emitted"], exact=True),
        row(
            "generator deficit",
            reference["generator_deficit"],
            run["generator_deficit"],
            exact=True,
        ),
        row(
            "coverage satisfied",
            reference["coverageSatisfied"],
            run["coverageSatisfied"],
            exact=True,
        ),
        row(
            "anomalies",
            ref_result.get("anomalies", []),
            run_result.get("anomalies", []),
            exact=True,
        ),
        row(
            "archive verified",
            ref_result.get("archiveVerified"),
            run_result.get("archiveVerified"),
            exact=True,
        ),
        row(
            "success response share",
            theirs["successResponse"] / theirs["emitted"],
            mine["successResponse"] / mine["emitted"],
        ),
    ]
    return rows


def proof_based(reference: dict, run: dict) -> list[dict]:
    """Quantities counted from proven requests only."""
    rows = []
    for key in ("local", "peer", "cloud"):
        rows.append(
            row(
                f"routing share {key}",
                shares(reference["routing"]).get(key, 0.0),
                shares(run["routing"]).get(key, 0.0),
            )
        )
    for node, outcomes in sorted(reference["edgeOutcomesByNode"].items()):
        mine = run["edgeOutcomesByNode"].get(node, {})
        for key in ("completed", "errors", "local", "peer"):
            rows.append(row(f"{node} {key}", outcomes.get(key, 0), mine.get(key, 0)))
    for origin, destinations in sorted(reference["originDestination"].items()):
        if origin == "cloud":
            continue
        mine = shares(run["originDestination"].get(origin, {}))
        for destination, share in sorted(shares(destinations).items()):
            if destination != "cloud":
                rows.append(
                    row(
                        f"{origin}→{destination} share",
                        share,
                        mine.get(destination, 0.0),
                    )
                )
    return rows


def recorded(reference: dict, run: dict) -> list[dict]:
    """Quantities that may differ: latency and cloud-served work."""
    rows = []
    for quantile in ("p50", "p95"):
        rows.append(
            row(
                f"k6 latency {quantile} (s)",
                (reference["latencySeconds"] or {}).get(quantile),
                (run["latencySeconds"] or {}).get(quantile),
            )
        )
    for key in ("reject", "httpError"):
        rows.append(
            row(
                f"{key} share",
                returned(reference)[key] / returned(reference)["emitted"],
                returned(run)[key] / returned(run)["emitted"],
            )
        )
    for key in ("completed", "errors", "planned"):
        rows.append(
            row(
                f"cloud direct {key} (proof-based)",
                (reference["cloudDirect"] or {}).get(key, 0),
                (run["cloudDirect"] or {}).get(key, 0),
            )
        )
    rows.append(
        row(
            "throughput originals/s",
            reference["throughputOriginalsPerSecond"],
            run["throughputOriginalsPerSecond"],
        )
    )
    rows.append(row("censored originals", reference["censored"], run["censored"]))

    def ready(report):
        values = [
            e["readySeconds"]
            for e in report.get("auctionEpochs", [])
            if not e.get("censored")
        ]
        return statistics.median(values) if values else None

    rows.append(row("median auction ready (s)", ready(reference), ready(run)))
    return rows


def markdown(document: dict) -> str:
    """Render the comparison as a review table."""

    def table(rows):
        lines = ["| Quantity | Reference | Run | Δ | OK |", "|---|---|---|---|---|"]
        for item in rows:
            difference = item.get("relativeDifference")
            delta = "" if difference is None else f"{difference:+.1%}"
            lines.append(
                f"| {item['quantity']} | {item['reference']} | {item['run']} | {delta} | {'✓' if item['ok'] else '✗'} |"
            )
        return "\n".join(lines)

    failing = [item["quantity"] for item in judged(document) if not item["ok"]]
    coverage = document["proofCoverage"]
    proofs = (
        f"Proof coverage: reference {coverage['reference']:.1%}, "
        f"run {coverage['run']:.1%} of successful responses; "
        + (
            "judged."
            if coverage["judged"]
            else f"below {PROOF_COVERAGE:.0%}, recorded only."
        )
    )
    sections = [
        "# One-shot comparison with the reference",
        f"Reference: `{document['reference']}`  \nRun: `{document['run']}`",
        f"Tolerance for must-match quantities: ±{TOLERANCE:.0%}.",
        "**Must-match quantities outside tolerance:** "
        + (", ".join(failing) if failing else "none"),
        "## Must match (edge behaviour)",
        table(document["mustMatch"]),
    ]
    if document["counters"] is not None:
        sections += [
            "## Must match: nanoFaaS counters per edge (every request)",
            table(document["counters"]["mustMatch"]),
        ]
    sections += [
        "## Proof-based (proven requests only)",
        proofs,
        table(document["proofBased"]),
        "## Recorded, not judged (network, cloud, admission)",
        table(document["recorded"] + (document["counters"] or {}).get("recorded", [])),
    ]
    if document["counters"] is None:
        sections.append(
            "Per-node counters not compared: no queue samples for both runs."
        )
    return "\n\n".join(sections) + "\n"


def judged(document: dict) -> list[dict]:
    """Rows that decide the verdict."""
    rows = list(document["mustMatch"])
    if document["counters"] is not None:
        rows += document["counters"]["mustMatch"]
    if document["proofCoverage"]["judged"]:
        rows += document["proofBased"]
    return rows


def main(argv=None):
    """Compare two run directories and write the review files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--reference-samples",
        type=Path,
        help="directory with the reference's queue-samples and schedule.json",
    )
    parser.add_argument(
        "--run-samples",
        type=Path,
        help="directory with the run's queue-samples and schedule.json",
    )
    args = parser.parse_args(argv)
    reference, ref_result = load(args.reference)
    run, run_result = load(args.run)
    sources = [args.reference_samples or args.reference, args.run_samples or args.run]
    counters = None
    if all(any(source.glob("queue-samples-*.jsonl")) for source in sources):
        judged_counters, noted = counter_rows(*map(node_counters, sources))
        counters = {"mustMatch": judged_counters, "recorded": noted}
    coverage = {"reference": proof_coverage(reference), "run": proof_coverage(run)}
    document = {
        "reference": str(args.reference),
        "run": str(args.run),
        "tolerance": TOLERANCE,
        "mustMatch": must_match(reference, run, ref_result, run_result),
        "counters": counters,
        "proofCoverage": {
            **coverage,
            "judged": min(coverage.values()) >= PROOF_COVERAGE,
        },
        "proofBased": proof_based(reference, run),
        "recorded": recorded(reference, run),
    }
    document["outsideTolerance"] = [
        item["quantity"] for item in judged(document) if not item["ok"]
    ]
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "comparison.json").write_text(json.dumps(document, indent=2) + "\n")
    (args.output / "comparison.md").write_text(markdown(document))
    print(args.output / "comparison.md")


if __name__ == "__main__":
    main()
