"""Returned outcomes are descriptive counts, independent of physical completions."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

CATEGORIES = (
    "successResponse",
    "reject",
    "httpError",
    "applicationError",
    "timeoutResponse",
    "invalidResponse",
    "unresolved",
    "missingResult",
    "notEmitted",
)
ERRORS = (
    "reject",
    "httpError",
    "applicationError",
    "timeoutResponse",
    "invalidResponse",
)


def classify_response(row: dict[str, Any]) -> str:
    """Separate explicit refusals from HTTP errors and uncertain timeout outcomes."""
    status = row.get("statusCode")
    response_status = str(row.get("responseStatus", "")).upper()
    try:
        body = json.loads(row.get("responseBody") or "{}")
    except (TypeError, ValueError):
        body = {}
    body = body if isinstance(body, dict) else {}
    error = body.get("error")
    target = row.get("offloadedTarget")
    proxy_refusal = False
    if status == 200 and response_status == "ERROR" and isinstance(error, dict):
        if error.get("code") == "EXTERNAL_TIMEOUT":
            return "timeoutResponse"
        if error.get("code") == "EXTERNAL_ERROR":
            proxy_refusal = error.get("message") == "Too many concurrent invocations"
            try:
                wrapped = json.loads(error.get("message") or "{}")
            except (ValueError, TypeError):
                wrapped = {}
            runtime_error = wrapped.get("error") if isinstance(wrapped, dict) else None
            if isinstance(runtime_error, dict):
                proxy_refusal = runtime_error.get("code") in {
                    "RUNTIME_HANDLER_SATURATED",
                    "RUNTIME_CALLBACK_SATURATED",
                }
    remote_refusal = (
        status == 502
        and error == "OFFLOAD_FAILED"
        and bool(target)
        and (
            body.get("message") == f"remote {target} returned 429"
            or str(body.get("message", "")).startswith(f"remote {target} returned 429:")
        )
    )
    if status == 429 or remote_refusal or proxy_refusal:
        return "reject"
    if not isinstance(status, int) or status == 0:
        return "unresolved"
    if status in (408, 504):
        return "timeoutResponse"
    if status >= 400:
        return "httpError"
    if response_status == "ERROR":
        return "applicationError"
    if status == 200 and response_status == "SUCCESS":
        return "successResponse"
    return "invalidResponse"


def _counts(
    planned: int, emitted: int, categories: Counter[str], duplicates: int
) -> dict[str, Any]:
    counts: dict[str, Any] = {category: categories[category] for category in CATEGORIES}
    counts.update(planned=planned, emitted=emitted, duplicateResultEvents=duplicates)
    counts["returnedErrors"] = sum(categories[key] for key in ERRORS)
    for key in (*ERRORS, "returnedErrors", "unresolved", "missingResult", "notEmitted"):
        for label, denominator in (("Planned", planned), ("Emitted", emitted)):
            counts[key + "PercentOf" + label] = (
                100 * counts[key] / denominator if denominator else None
            )
    return counts


def summarize_responses(
    planned: list[dict[str, Any]], events: list[dict[str, Any]], *, mode: str
) -> dict[str, Any]:
    """Count each scheduled original once, retaining missing and conflicting results."""
    scheduled = {row["originalId"]: row for row in planned}
    emitted = {row["originalId"] for row in events if row.get("event") == "emitted"}
    results: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        if row.get("event") == "result":
            results[row["originalId"]].append(row)
    records = []
    details = []
    for original, row in scheduled.items():
        responses = results.get(original, [])
        category = (
            "notEmitted"
            if original not in emitted and not responses
            else "missingResult"
            if not responses
            else classify_response(responses[0])
        )
        # Repeated identical log events do not inflate counts. Conflicting responses
        # cannot establish a single returned outcome for an original.
        if (
            len(responses) > 1
            and len({json.dumps(response, sort_keys=True) for response in responses})
            > 1
        ):
            category = "unresolved"
        records.append(
            dict(
                row,
                category=category,
                emitted=original in emitted,
                duplicateResultEvents=max(0, len(responses) - 1),
            )
        )
        if responses and category != "successResponse" and len(details) < 100:
            details.append(dict(responses[0], category=category))
    phases = {}
    for phase in ("warmup", "campaign"):
        selected = [row for row in records if row.get("phase") == phase]
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for row in selected:
            groups[(row["origin"], row["function"], row["epoch"])].append(row)

        def count(rows: list[dict]) -> dict[str, Any]:
            return _counts(
                len(rows),
                sum(row["emitted"] for row in rows),
                Counter(row["category"] for row in rows),
                sum(row["duplicateResultEvents"] for row in rows),
            )

        phases[phase] = {
            "total": count(selected),
            "byCell": [
                dict(origin=key[0], function=key[1], epoch=key[2], **count(rows))
                for key, rows in sorted(groups.items())
            ],
        }
        for dimension, label in (
            ("origin", "Node"),
            ("function", "Function"),
            ("epoch", "Epoch"),
        ):
            grouped: dict[str, list[dict]] = defaultdict(list)
            for row in selected:
                grouped[str(row[dimension])].append(row)
            phases[phase]["by" + label] = {
                key: count(rows) for key, rows in sorted(grouped.items())
            }
    return {
        "mode": mode,
        "phases": phases,
        "denominator": (
            "scheduled originals, separately for warmup and campaign; "
            "includes cloud-direct"
        ),
        "successConvention": (
            "successful HTTP/application response; physical completion "
            "and checksum verified separately"
        ),
        "errorDetails": details,
        "errorDetailsLimit": 100,
        "allResponsesArtifact": "generator.jsonl",
        "unexpectedOriginals": len((emitted | set(results)) - set(scheduled)),
        "duplicatePlannedOriginals": len(planned) - len(scheduled),
    }
