"""Reconcile the mixed generator's response ledger with k6 totals."""

import json
from collections import Counter
from pathlib import Path

from nanolab.comparison.experiment_01 import read_events


def write_http_accounting(run_dir: Path) -> dict:
    """Write exact response counts and reject incomplete generator evidence."""
    events, malformed = read_events(run_dir / "k6-events.jsonl")
    metrics = json.loads((run_dir / "k6-summary.json").read_text())["metrics"]
    fields = ("fn", "path", "idem", "part", "status", "error_code")
    counts = Counter(
        (
            e["fn"],
            e["path"],
            e["idem"],
            e["part"],
            e["status"],
            e.get("error_code") or 0,
        )
        for e in events
        if e.get("t") == "http"
    )
    rows = [
        dict(zip(fields, key, strict=True), count=count)
        for key, count in sorted(counts.items())
    ]
    total = sum(counts.values())
    failed = sum(r["count"] for r in rows if not 200 <= r["status"] <= 399)
    unexpected = sum(
        r["count"]
        for r in rows
        if r["status"]
        not in (
            (200,)
            if r["path"] == "probe"
            else (202, 429, 503)
            if r["path"] == "async"
            else (200, 429, 503)
        )
    )
    result = {
        "rows": rows,
        "recorded_http": total,
        "k6_http": metrics["http_reqs"]["count"],
        "recorded_failed_http": failed,
        "k6_failed_http": metrics["http_req_failed"]["passes"],
        "unexpected_http": unexpected,
        "malformed_lines": malformed,
    }
    (run_dir / "http-status-counts.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    if malformed or total != result["k6_http"] or failed != result["k6_failed_http"]:
        raise RuntimeError(
            "Mixed HTTP ledger does not reconcile with k6; "
            "retained http-status-counts.json"
        )
    return result
