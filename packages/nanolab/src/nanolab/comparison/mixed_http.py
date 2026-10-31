"""Reconcile the mixed generator's response ledger with k6 totals."""

import json
from collections import Counter
from pathlib import Path

from nanolab.comparison.experiment_01 import read_events


def write_http_accounting(run_dir: Path) -> dict:
    """Write exact response counts and reject incomplete generator evidence."""
    events, malformed = read_events(run_dir / "k6-events.jsonl")
    metrics = json.loads((run_dir / "k6-summary.json").read_text())["metrics"]
    b5 = "b5_offered" in metrics
    fields = ("fn", "path", "idem", "part", "status", "error_code")
    if b5:
        fields += ("phase", "outcome")
    counts = Counter(
        (
            e["fn"],
            e["path"],
            e["idem"],
            e["part"],
            e["status"],
            e.get("error_code") or 0,
        )
        + ((e.get("phase", ""), e.get("outcome", "")) if b5 else ())
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
    body_mismatch = False
    if b5:
        outcomes = ("valid_completed", "refused", "invalid_output", "http_error")
        measured = [
            e
            for e in events
            if e.get("t") == "http" and e.get("phase") == "measurement"
        ]
        observed = Counter(e.get("outcome") for e in measured)
        measurement = {
            "offered": len(measured),
            **{name: observed[name] for name in outcomes},
        }
        result["measurement"] = measurement
        result["k6_measurement"] = {
            name: metrics.get(f"b5_{name}", {}).get("count") for name in measurement
        }
        body_mismatch = (
            measurement != result["k6_measurement"]
            or sum(observed[name] for name in outcomes) != len(measured)
            or any(
                e.get("phase") not in ("qualification", "measurement")
                for e in events
                if e.get("t") == "http"
            )
        )
    (run_dir / "http-status-counts.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    if (
        body_mismatch
        or malformed
        or total != result["k6_http"]
        or failed != result["k6_failed_http"]
    ):
        raise RuntimeError(
            "HTTP ledger does not reconcile with k6; retained http-status-counts.json"
        )
    return result
