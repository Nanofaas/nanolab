"""Atomic immutable publication and canonical evidence encoding."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    """Encode reproducible UTF-8 JSON with finite numbers and sorted object keys."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def content_hash(content: bytes) -> str:
    """Return the unprefixed artifact SHA-256."""
    return hashlib.sha256(content).hexdigest()


def write_immutable_artifact(path: Path, content: bytes) -> str:
    """Publish fully written bytes without overwriting any concurrent publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        temporary = Path(staged.name)
        try:
            staged.write(content)
            staged.flush()
            os.fsync(staged.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise ValueError(
                        f"immutable artifact already differs: {path}"
                    ) from None
        finally:
            temporary.unlink()
    return content_hash(content)


def append_observation(path: Path, value: Any) -> None:
    """Persist every raw observation before processing or further invocations."""
    with path.open("ab") as evidence:
        evidence.write(canonical_bytes(value) + b"\n")
        evidence.flush()
