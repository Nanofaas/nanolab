"""NanoLab evidence receipts over Sonata's working-tree source snapshots."""

from __future__ import annotations

from pathlib import Path

from sonata_tasks.sources import SourceChangedError as SourceChangedError
from sonata_tasks.sources import SourceEntry as SourceEntry
from sonata_tasks.sources import SourceSnapshot as SourceSnapshot
from sonata_tasks.sources import capture_source_snapshot as _capture
from sonata_tasks.sources import materialize_snapshot as materialize_snapshot
from sonata_tasks.sources import source_entry
from sonata_tasks.sources import verify_snapshot as verify_snapshot

from nanolab.tasks.soak.artifacts import ArtifactWriter

# Retain the former internal inspector name for existing callers.
_entry = source_entry


def capture_source_snapshot(
    repo_root: Path, destination: Path, *, max_bytes: int
) -> SourceSnapshot:
    """Capture shared inputs while retaining NanoLab ownership and receipt policy."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("source byte budget must be a positive integer")
    root = repo_root.resolve()
    output = destination.absolute()
    if output.resolve().is_relative_to(root):
        raise ValueError("snapshot destination must be outside the source checkout")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"snapshot destination already exists: {output}")
    output.mkdir(parents=True, exist_ok=False)
    writer = ArtifactWriter(output, limit_bytes=16 * 1024 * 1024)
    try:
        snapshot = _capture(root, writer, max_bytes=max_bytes)
        writer.write_json(
            "snapshot.json",
            {
                "schema": "nanolab-soak-v1",
                "status": "captured",
                "source_root": str(root),
                "root": str(snapshot.root),
                "revision": snapshot.revision,
                "dirty": snapshot.dirty,
                "fingerprint": snapshot.fingerprint,
                "manifest_sha256": snapshot.manifest_sha256,
                "entry_count": len(snapshot.entries),
            },
        )
        return snapshot
    finally:
        writer.close()
