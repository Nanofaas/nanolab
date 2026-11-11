"""Physical receipts from logs, independent of replica lifetime."""

from __future__ import annotations

import json
import math
import time

import httpx

from nanolab.one_shot.returned_errors import classify_response
from nanolab.tasks.one_shot.artifacts import append_observation
from nanolab.tasks.one_shot.logging import export_log_events, query_url


def receipt_endpoint(run) -> str | None:
    """Return the operator's Loki endpoint only for a journal-enabled platform."""
    return getattr(
        getattr(getattr(run, "topology", None), "platform", None),
        "receipt_endpoint",
        None,
    )


def correlate_receipts(events, rows, *, run_id, nodes, endpoints) -> list[dict]:
    """Join terminal identities and reject contradictory receipts."""
    originals = {}
    for row in rows:
        if row.get("event") != "result" or row.get("phase") != "campaign":
            continue
        identity = row.get("terminalExecutionId") or row.get("executionId")
        if identity:
            previous = originals.setdefault(identity, row)
            if previous != row:
                raise ValueError("ambiguous terminal execution identity")
    proofs = {}
    for event in events:
        if event.get("runId") != run_id:
            continue
        receipt = event.get("attributes", {})
        if receipt.get("event") != "runtime.execution.released":
            continue
        identity = event.get("executionId")
        row = originals.get(identity)
        if row is None:
            continue
        destination = event.get("nodeId")
        expected = row.get("executionNode")
        if expected is None:
            target = row.get("offloadedTarget")
            expected = (
                next(
                    (
                        node
                        for node, url in endpoints.items()
                        if url.rstrip("/") == (target or "").rstrip("/")
                    ),
                    None,
                )
                if target
                else row.get("origin")
            )
        if destination not in nodes or destination != expected:
            raise ValueError("receipt destination differs from client outcome")
        seconds = receipt.get("occupancySeconds")
        if (
            type(event.get("schemaVersion")) is not int
            or event["schemaVersion"] != 1
            or type(receipt.get("receiptSchemaVersion")) is not int
            or receipt["receiptSchemaVersion"] != 1
            or receipt.get("state") != "RELEASED"
            or not isinstance(receipt.get("incarnation"), str)
            or not receipt["incarnation"]
            or receipt.get("dispatchAttempt") != "1"
            or type(receipt.get("handlerStarted")) is not bool
            or type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or seconds < 0
            or receipt.get("responseStatus")
            not in {"success", "error", "timeout", "cancelled"}
        ):
            raise ValueError("invalid physical release receipt")
        proof = {
            **row,
            **{
                key: receipt[key]
                for key in (
                    "incarnation",
                    "dispatchAttempt",
                    "state",
                    "occupancySeconds",
                    "responseStatus",
                    "handlerStarted",
                )
            },
            "executionId": identity,
            "destination": destination,
            "evidenceSource": "loki",
        }
        key = (
            identity,
            receipt["incarnation"],
            receipt["dispatchAttempt"],
            destination,
        )
        previous = proofs.setdefault(key, proof)
        if previous != proof:
            raise ValueError("conflicting physical release receipts")
    return list(proofs.values())


def collect_log_receipts(run, inputs, rows, *, deadline: float) -> None:
    """Wait for ingestion within a deadline and preserve incomplete evidence."""
    endpoint = receipt_endpoint(run)
    if endpoint is None:
        raise ValueError("receipt log endpoint unavailable")
    results = [
        row
        for row in rows
        if row.get("event") == "result" and row.get("phase") == "campaign"
    ]
    if not results:
        return
    pending = {
        row["originalId"] for row in results if classify_response(row) != "reject"
    }
    start = int(min(row["scheduledAt"] for row in results) * 1e9) - 1_000_000_000
    query = (
        '{job="nanofaas"} | json | runId='
        + json.dumps(run.run_dir.name)
        + ' | attributes_event="runtime.execution.released"'
    )
    raw = run.run_dir / "receipt-events.jsonl"
    endpoints = {
        node: inputs.resource(endpoint)
        for node, endpoint in run.topology.endpoints.items()
    }
    written = set()
    with httpx.Client(trust_env=False) as http:
        while True:
            export_log_events(
                http,
                query_url(endpoint),
                query,
                start,
                time.time_ns(),
                run.settings.observability.logs.export_limit,
                raw,
            )
            events = (
                [json.loads(line) for line in raw.read_text().splitlines()]
                if raw.exists()
                else []
            )
            proofs = correlate_receipts(
                events,
                results,
                run_id=run.run_dir.name,
                nodes=set(endpoints),
                endpoints=endpoints,
            )
            for proof in proofs:
                key = (
                    proof["executionId"],
                    proof["incarnation"],
                    proof["dispatchAttempt"],
                    proof["destination"],
                )
                if key not in written:
                    append_observation(run.run_dir / "physical.jsonl", proof)
                    written.add(key)
                pending.discard(proof["originalId"])
            if not pending or time.monotonic() >= deadline:
                append_observation(
                    run.run_dir / "receipt-collection.jsonl",
                    {
                        "receipts": len(proofs),
                        "missingReleaseOriginals": sorted(pending),
                        "complete": not pending,
                    },
                )
                return
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
