#!/usr/bin/env python3
"""Compare a one-shot observation run with a reference run of the same scenario.

Splits the comparison as the multi-site design does: edge-side behaviour must
match the reference within run-to-run noise; network latency and anything the
cloud serves may differ (different site, architecture or location) and is only
recorded. Reads each run directory's report.json and result.json; writes
comparison.json and comparison.md into --output.
"""

import argparse
import json
import statistics
from pathlib import Path

# Relative difference beyond which a must-match quantity is flagged for review.
TOLERANCE = 0.10


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


def shares(counts: dict[str, int]) -> dict[str, float]:
    """Normalize counts to shares of their total."""
    total = sum(counts.values())
    return {key: value / total for key, value in counts.items()} if total else {}


def must_match(reference: dict, run: dict, ref_result: dict, run_result: dict):
    """Edge-side quantities expected to agree within run-to-run noise."""
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
    ]
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
    for key in ("completed", "errors", "planned"):
        rows.append(
            row(
                f"cloud direct {key}",
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

    failing = [item["quantity"] for item in document["mustMatch"] if not item["ok"]]
    return (
        "\n\n".join(
            [
                "# One-shot comparison with the reference",
                f"Reference: `{document['reference']}`  \nRun: `{document['run']}`",
                f"Tolerance for must-match quantities: ±{TOLERANCE:.0%}.",
                "**Must-match quantities outside tolerance:** "
                + (", ".join(failing) if failing else "none"),
                "## Must match (edge behaviour)",
                table(document["mustMatch"]),
                "## Recorded, not judged (network and cloud)",
                table(document["recorded"]),
            ]
        )
        + "\n"
    )


def main(argv=None):
    """Compare two run directories and write the review files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    reference, ref_result = load(args.reference)
    run, run_result = load(args.run)
    document = {
        "reference": str(args.reference),
        "run": str(args.run),
        "tolerance": TOLERANCE,
        "mustMatch": must_match(reference, run, ref_result, run_result),
        "recorded": recorded(reference, run),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "comparison.json").write_text(json.dumps(document, indent=2) + "\n")
    (args.output / "comparison.md").write_text(markdown(document))
    print(args.output / "comparison.md")


if __name__ == "__main__":
    main()
