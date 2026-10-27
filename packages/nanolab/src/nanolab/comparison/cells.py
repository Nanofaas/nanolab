"""The layout every comparison cell shares: attempts, artefacts and receipts.

Each cell carries an atomic receipt, `cell-result.json`, naming the attempt it
selects, a definitive outcome and the hashes of the evidence behind it. Only a
valid receipt makes a cell complete; an interruption before it is written leaves
the cell incomplete, and the next attempt starts in a fresh directory beside the
old ones rather than on top of them. Experiments 01 and 02 both run their cells
this way; what they measure inside a cell is their own.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from nanolab.comparison.matrix import MatrixCell

CELL_RESULT = "cell-result.json"
EVENTS = "k6-events.jsonl"
SUMMARY = "k6-summary.json"
SNAPSHOT = "metrics/prometheus-snapshot.json"
LOAD_STARTED = "load-started.json"
DIAGNOSTICS = "diagnostics"

Outcome = Literal["measured", "startup_failed", "oom"]
DEFINITIVE: frozenset[str] = frozenset({"measured", "startup_failed", "oom"})


def sha256_file(path: Path) -> str:
    """Return the hex sha256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity_sha256(identity: Mapping[str, object]) -> str:
    """Hash a manifest identity the way receipts refer to it."""
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json_atomically(path: Path, value: object) -> None:
    """Write `value` as JSON through a synced temporary file and a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------- attempts


def attempt_dir(cell_dir: Path, attempt: int) -> Path:
    """Return the directory one attempt of a cell writes into."""
    return cell_dir / "attempts" / str(attempt)


def next_attempt(cell_dir: Path) -> int:
    """Return a fresh attempt number; earlier attempts are never reused."""
    root = cell_dir / "attempts"
    numbers = (
        [int(path.name) for path in root.iterdir() if path.name.isdigit()]
        if root.is_dir()
        else []
    )
    return max(numbers, default=0) + 1


def archive_cell_result(cell_dir: Path) -> None:
    """Leave a replacement pending while retaining the previous receipt."""
    path = cell_dir / CELL_RESULT
    if path.is_file():
        receipt = json.loads(path.read_text(encoding="utf-8"))
        path.replace(attempt_dir(cell_dir, int(receipt["attempt"])) / CELL_RESULT)


def mark_load_started(run_dir: Path) -> None:
    """Record that every pre-load check passed and the generator is next.

    After this marker an error is no longer a transport failure before load: a
    retry could re-measure a system the first attempt already loaded.
    """
    write_json_atomically(run_dir / LOAD_STARTED, {"at": datetime.now(UTC).isoformat()})


# ---------------------------------------------------------------- receipts


def write_cell_result(
    cell: MatrixCell,
    root: Path,
    *,
    identity: Mapping[str, object],
    attempt: int,
    outcome: Outcome,
    reason: str,
    started_at: datetime,
    required: tuple[str, ...] = (SUMMARY, EVENTS, SNAPSHOT),
) -> dict[str, object]:
    """Commit a definitive outcome for a cell, atomically, with its evidence.

    `measured` requires every artefact in `required`: by default the summary,
    the event ledger and the snapshot. A failure requires diagnostics and names
    which of those artefacts it could not produce; none is invented to fill the
    gap.
    """
    selected = attempt_dir(cell.run_dir(root), attempt)
    needed = required if outcome == "measured" else ()
    missing = [name for name in needed if not (selected / name).is_file()]
    if missing:
        raise ValueError(f"measured outcome lacks evidence: {', '.join(missing)}")
    diagnostics = selected / DIAGNOSTICS
    if outcome != "measured" and (
        not diagnostics.is_dir() or not any(diagnostics.iterdir())
    ):
        raise ValueError(f"{outcome} outcome requires diagnostic evidence")
    evidence = {
        path.relative_to(selected).as_posix(): sha256_file(path)
        for path in sorted(selected.rglob("*"))
        if path.is_file() and not path.name.startswith(".")
    }
    receipt: dict[str, object] = {
        "schemaVersion": 1,
        "cell": cell.label,
        "variant": cell.key,
        "repetition": cell.repetition,
        "identitySha256": identity_sha256(identity),
        "attempt": attempt,
        "outcome": outcome,
        "reason": reason,
        "startedAt": started_at.isoformat(),
        "recordedAt": datetime.now(UTC).isoformat(),
        "evidence": evidence,
        "notProduced": [name for name in required if not (selected / name).is_file()],
    }
    write_json_atomically(cell.run_dir(root) / CELL_RESULT, receipt)
    return receipt


def read_cell_result(
    cell: MatrixCell, root: Path, identity: Mapping[str, object]
) -> dict[str, Any] | None:
    """Return a cell's receipt if it is definitive and its evidence intact."""
    path = cell.run_dir(root) / CELL_RESULT
    if not path.is_file():
        return None
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if (
        receipt.get("outcome") not in DEFINITIVE
        or receipt.get("cell") != cell.label
        or receipt.get("identitySha256") != identity_sha256(identity)
    ):
        raise ValueError(f"{cell.label}: receipt does not belong to this run")
    selected = attempt_dir(cell.run_dir(root), int(receipt["attempt"]))
    for name, digest in receipt["evidence"].items():
        evidence = selected / name
        if not evidence.is_file() or sha256_file(evidence) != digest:
            raise ValueError(f"{cell.label}: receipt evidence missing or changed")
    return receipt


def experiment_completed(
    identity: Mapping[str, object],
) -> Any:
    """Return the completion rule `matrix.pending` applies to a receipted run."""

    def done(cell: MatrixCell, root: Path) -> bool:
        return read_cell_result(cell, root, identity) is not None

    return done
