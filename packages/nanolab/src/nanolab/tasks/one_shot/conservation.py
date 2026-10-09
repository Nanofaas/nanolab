"""Conservation counts original IDs separately from physical dispatch attempts."""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict, JsonValue


class RunEvidence(BaseModel):
    """Measured counts, deficits and explicit failure reasons for one run."""

    model_config = ConfigDict(extra="forbid")
    planned: int
    emitted: int
    received: int
    attempts: int
    completed: int
    errors: int
    censored: int = 0
    generator_deficit: int
    valid: bool
    violations: list[str]
    cells: list[dict[str, JsonValue]]
    observations: dict[str, JsonValue] = {}


def evaluate_conservation(
    planned: list[dict], emitted: list[dict], attempts: list[dict], results: list[dict]
) -> RunEvidence:
    """Require a real, complete run and at most one physical attempt per original."""
    expected = {row["originalId"] for row in planned}
    sent = {row["originalId"] for row in emitted}
    received = {row["originalId"] for row in attempts if row.get("handlerStarted")}
    successes = {row["originalId"] for row in results if row.get("success")}
    released = {
        row["originalId"]
        for row in attempts
        if row.get("state") == "RELEASED"
        and str(row.get("responseStatus", "")).upper() == "SUCCESS"
        and row.get("handlerStarted")
    }
    completed = expected & successes & released
    terminal_errors = {
        row["originalId"]
        for row in results
        if not row.get("success") and row.get("terminalError")
    } & expected
    outcomes = {row["originalId"] for row in results}
    counts = Counter(row["originalId"] for row in attempts)
    violations = []
    if not expected or not sent:
        violations.append("no load was executed")
    if len(expected) != len(planned) or len(sent) != len(emitted):
        violations.append("duplicate original emission or schedule")
    if sent != expected:
        violations.append("generator deficit or unexpected originals")
    if any(count > 1 for count in counts.values()):
        violations.append("multiple physical attempts for an original")
    proof_owners: dict[tuple, str] = {}
    for row in attempts:
        if row.get("executionId") and row.get("incarnation"):
            identity = (
                row["executionId"],
                row["incarnation"],
                row.get("dispatchAttempt"),
            )
            owner = proof_owners.setdefault(identity, row["originalId"])
            if owner != row["originalId"]:
                violations.append("physical proof reused for different originals")
    if not completed <= received or completed | terminal_errors != expected:
        violations.append("missing physical receipt/completion or unresolved outcome")
    if len(outcomes) != len(results):
        violations.append("duplicate terminal result")
    if set(counts) - expected or outcomes - expected:
        violations.append("unplanned execution/result")
    cells = []
    keys = sorted({(row["origin"], row["function"], row["epoch"]) for row in planned})
    for origin, function, epoch in keys:
        ids = {
            row["originalId"]
            for row in planned
            if (row["origin"], row["function"], row["epoch"])
            == (origin, function, epoch)
        }
        destinations = Counter(
            row.get("destination", "unknown")
            for row in attempts
            if row["originalId"] in ids and row["originalId"] in completed
        )
        cells.append(
            {
                "origin": origin,
                "function": function,
                "epoch": epoch,
                "planned": len(ids),
                "emitted": len(ids & sent),
                "completed": len(ids & completed),
                "errors": len(ids & terminal_errors),
                "destinations": dict(destinations),
            }
        )
    return RunEvidence(
        planned=len(expected),
        emitted=len(sent),
        received=len(received),
        attempts=len(attempts),
        completed=len(completed),
        errors=len(terminal_errors),
        censored=len(sent - completed - terminal_errors),
        generator_deficit=len(expected - sent),
        valid=not violations,
        violations=violations,
        cells=cells,
    )
