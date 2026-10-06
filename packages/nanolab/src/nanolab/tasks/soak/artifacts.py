"""NanoLab evidence policy over Sonata's bounded artifact storage."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

from sonata_tasks.artifacts import MAX_RECORD_BYTES as MAX_RECORD_BYTES
from sonata_tasks.artifacts import ArtifactCorruptionError as ArtifactCorruptionError
from sonata_tasks.artifacts import (
    ArtifactLimitExceededError as ArtifactLimitExceededError,
)
from sonata_tasks.artifacts import ArtifactWriter as _SharedWriter
from sonata_tasks.artifacts import IncompleteRecordError
from sonata_tasks.artifacts import describe_artifact as describe_artifact
from sonata_tasks.artifacts import encode_record as encode_record
from sonata_tasks.artifacts import read_records as _read_records

# Producers keep this space clear; the writer itself reserves min(4096, limit / 8).
TERMINAL_RESERVE = 4096


def fingerprint(value: dict[str, object]) -> str:
    """Keep strict JSON inputs and existing bare-hex canonical fingerprints."""
    return hashlib.sha256(encode_record(value)[:-1]).hexdigest()


def retained(root: Path, path: Path) -> bool:
    """Whether the acceptance inventory names and charges for ``path``.

    One rule, applied the same way whether the inventory lists a file on its own
    or measures a whole tree under one entry, so grouping a subtree can never
    move the byte total it feeds `budget_exhausted` with. Build workspaces and
    dot-directories are scratch the run does not retain as evidence, which is
    also the exclusion `measure_tree` applies to the budget; the captured source
    tree is inventoried file by file in `source-manifest.jsonl`, which
    `snapshot.json` seals, and both of those stay in the inventory.
    """
    relative = path.relative_to(root)
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(part.startswith(("workspace-", ".")) for part in relative.parts)
        and not path.is_relative_to(root / "source" / "tree")
    )


def describe_tree(root: Path, path: Path) -> dict[str, Any]:
    """Hash a whole retained tree as one artifact reference.

    A helper's command-log tree holds thousands of `docker-*.log` files, and the
    acceptance inventory is a single JSON record with a bounded size, so it
    cannot list them one by one. Binding the tree as one entry keeps every file
    hashed and counted: the digest covers each retained file's name and
    contents, and `size_bytes` is the exact total the per-file entries carried.
    """
    digest = hashlib.sha256()
    size = 0
    for item in sorted(path.rglob("*")):
        if not retained(root, item):
            continue
        digest.update(item.relative_to(path).as_posix().encode("utf-8") + b"\0")
        with item.open("rb") as source:
            while chunk := source.read(65536):
                size += len(chunk)
                digest.update(chunk)
    return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


def measure_tree(root: Path) -> int:
    """Measure every generated file under ``root``, not only owned evidence.

    Build workspaces and dot-files are excluded, matching what the run actually
    retains: the same exclusion the acceptance inventory uses. Excluded
    directories are pruned during the walk, so a build workspace costs one
    directory entry rather than one `stat` per file beneath it, and the walk
    never follows a symbolic link (as `Path.rglob` does not).
    """
    total = 0
    pending = [str(root)]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.name.startswith(("workspace-", ".")):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
    return total


def enforce_limit(root: Path, limit_bytes: int) -> int:
    """Bound the run's total artifacts while it can still be stopped.

    Per-producer limits bound each journal, log and dump on its own; only this
    measurement bounds their sum, which is what the run budget actually promises.
    """
    total = measure_tree(root)
    if total > limit_bytes:
        raise ArtifactLimitExceededError(
            f"cumulative run artifacts {total} exceed the {limit_bytes} byte budget"
        )
    return total


class ArtifactWriter:
    """Apply terminal reservation and run accounting to shared storage.

    Serialized producers may share ``budget_root`` to include direct writes and
    sibling owners in each pre-write quota check. Default accounting is local.
    """

    def __init__(
        self, root: Path, limit_bytes: int, *, budget_root: Path | None = None
    ) -> None:
        """Preserve NanoLab's exclusive directory and terminal-report policy."""
        if type(limit_bytes) is not int or limit_bytes < 128:
            raise ValueError("artifact limit must be an integer of at least 128 bytes")
        usage = partial(measure_tree, budget_root) if budget_root is not None else None
        self._storage = _SharedWriter(
            root,
            limit_bytes,
            owner_marker=".soak-owner",
            reserve_bytes=min(TERMINAL_RESERVE, limit_bytes // 8),
            measure_usage=usage,
        )
        self.root = self._storage.root
        self.limit_bytes = limit_bytes
        self.budget_root = budget_root

    def append(self, stream: str, record: dict[str, object]) -> None:
        """Persist a record using the shared quota and partial-write accounting."""
        self._storage.append(stream, record)

    def write_json(self, name: str, value: dict[str, object]) -> Path:
        """Only the terminal report can consume the reserved capacity."""
        return self._storage.write_json(
            name, value, use_reserve=name == "terminal.json"
        )

    def write_blob(self, directory: str, name: str, body: bytes) -> Path:
        """Publish immutable raw evidence under the shared byte budget."""
        return self._storage.write_blob(directory, name, body)

    def write_file(self, name: str, body: bytes) -> Path:
        """Publish immutable raw input directly under the evidence owner."""
        return self._storage.write_file(name, body)

    def close(self) -> None:
        """Stop future writes while retaining evidence and the ownership marker."""
        self._storage.close()


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Turn only a torn final record into NanoLab's observation-gap evidence."""
    try:
        yield from _read_records(path)
    except IncompleteRecordError as error:
        yield {
            "schema": "nanolab-soak-v1",
            "kind": "observation_gap",
            "line": error.line_number,
            "reason": "incomplete final record",
        }
