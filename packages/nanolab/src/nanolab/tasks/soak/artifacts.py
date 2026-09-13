"""Incremental, bounded evidence storage that does not overwrite prior runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from threading import Lock
from typing import Any

MAX_RECORD_BYTES = 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class ArtifactLimitExceededError(OSError):
    """Evidence could not be written without exceeding its declared disk budget."""


class ArtifactCorruptionError(ValueError):
    """A complete evidence record is malformed and cannot be silently discarded."""


def _encode(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def fingerprint(value: dict[str, object]) -> str:
    """Hash canonical JSON inputs, independent of mapping insertion order."""
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def describe_artifact(path: Path) -> dict[str, object]:
    """Hash an artifact with fixed memory usage, including its byte count."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(65536):
            size += len(chunk)
            digest.update(chunk)
    return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


def measure_tree(root: Path) -> int:
    """Measure every generated file under ``root``, not only owned evidence.

    Build workspaces and dot-files are excluded, matching what the run actually
    retains: the same exclusion the acceptance inventory uses.
    """
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not any(
            part.startswith(("workspace-", "."))
            for part in path.relative_to(root).parts
        )
    )


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
    """Own a run directory and persist observations without retaining their history."""

    def __init__(self, root: Path, limit_bytes: int) -> None:
        """Exclusively acquire an empty directory and reserve terminal-report space."""
        if type(limit_bytes) is not int or limit_bytes < 128:
            raise ValueError("artifact limit must be an integer of at least 128 bytes")
        if root.is_symlink():
            raise ValueError("artifact root cannot be a symbolic link")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(root.iterdir()):
            raise FileExistsError(f"run directory already contains evidence: {root}")
        # O_EXCL resolves the race between two owners both seeing an empty directory.
        descriptor = os.open(
            root / ".soak-owner", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        os.close(descriptor)
        self.root = root
        self.limit_bytes = limit_bytes
        self._reserve = min(4096, limit_bytes // 8)
        self._used_bytes = 0
        self._closed = False
        self._lock = Lock()

    def _target(self, name: str) -> Path:
        if _NAME.fullmatch(name) is None:
            raise ValueError("artifact name must be a single safe path component")
        return self.root / name

    def _check_write(self, size: int, *, terminal: bool = False) -> None:
        if self._closed:
            raise RuntimeError("artifact writer is closed")
        if size > MAX_RECORD_BYTES:
            raise ArtifactLimitExceededError(
                "individual evidence record exceeds its size limit"
            )
        budget = self.limit_bytes if terminal else self.limit_bytes - self._reserve
        if self._used_bytes + size > budget:
            raise ArtifactLimitExceededError(
                "artifact budget exhausted; terminal space is reserved"
            )

    def append(self, stream: str, record: dict[str, object]) -> None:
        """Append one complete JSONL record, accounting for partial writes."""
        target = self._target(stream)
        target = target.with_name(target.name + ".jsonl")
        payload = _encode(record)
        with self._lock:
            self._check_write(len(payload))
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "ab", buffering=0) as output:
                before = os.fstat(output.fileno()).st_size
                try:
                    remaining = memoryview(payload)
                    while remaining:
                        written = output.write(remaining)
                        if written is None or written <= 0:
                            raise OSError("evidence append made no progress")
                        remaining = remaining[written:]
                finally:
                    self._used_bytes += max(
                        0, os.fstat(output.fileno()).st_size - before
                    )

    def write_json(self, name: str, value: dict[str, object]) -> Path:
        """Publish a new immutable document, never replacing an evaluation."""
        target = self._target(name)
        if target.suffix != ".json":
            raise ValueError("complete JSON artifact names must end with .json")
        payload = _encode(value)
        with self._lock:
            self._check_write(len(payload), terminal=name == "terminal.json")
            descriptor, filename = tempfile.mkstemp(prefix=".pending-", dir=self.root)
            temporary = Path(filename)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                # A hard link publishes complete content atomically and fails if an
                # artifact (including a symbolic link) already owns the target name.
                os.link(temporary, target)
                self._used_bytes += len(payload)
            finally:
                temporary.unlink(missing_ok=True)
        return target

    def close(self) -> None:
        """Idempotently stop future writes; evidence and ownership marker remain."""
        with self._lock:
            self._closed = True


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Stream evidence: a torn tail is a gap, other corruption is an error."""
    with path.open("rb") as source:
        line_number = 0
        while payload := source.readline(MAX_RECORD_BYTES + 1):
            line_number += 1
            if len(payload) > MAX_RECORD_BYTES:
                raise ArtifactCorruptionError(
                    f"{path}:{line_number}: record exceeds size limit"
                )
            if not payload.endswith(b"\n"):
                yield {
                    "schema": "nanolab-soak-v1",
                    "kind": "observation_gap",
                    "line": line_number,
                    "reason": "incomplete final record",
                }
                return
            try:
                record = json.loads(payload, parse_constant=_reject_constant)
            except (ValueError, UnicodeDecodeError) as error:
                raise ArtifactCorruptionError(
                    f"{path}:{line_number}: malformed record"
                ) from error
            if not isinstance(record, dict):
                raise ArtifactCorruptionError(
                    f"{path}:{line_number}: record must be an object"
                )
            yield record
