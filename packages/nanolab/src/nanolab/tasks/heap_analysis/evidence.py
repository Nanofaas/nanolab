"""Bounded raw memory evidence and a comparison with explicit missing entries."""

import json
import os
import tempfile
from pathlib import Path

from nanolab.tasks.heap_analysis.native import summarize
from nanolab.tasks.soak.artifacts import (
    MAX_RECORD_BYTES,
    ArtifactLimitExceededError,
    describe_artifact,
    enforce_limit,
)

CHECKPOINTS = {
    "before-baseline": "after warmup, before baseline GC and dump",
    "natural-drain": "after natural drain, before final GC",
    "after-final-gc": "after completed explicit final GC, before final dump",
}
_RAW = {
    "status": ("status.txt", 65536),
    "smaps_rollup": ("smaps-rollup.txt", 262144),
    "smaps": ("smaps.txt", 8388608),
    "heap_info": ("heap-info.txt", 2097152),
}
_TERMINAL_RESERVE = 4096


def _write_raw(root: Path, name: str, body: bytes, run_limit: int) -> dict:
    """Publish once, accounting for all retained artifacts in this serial session."""
    run_root = root.parent
    used = enforce_limit(run_root, run_limit)
    if used + len(body) + _TERMINAL_RESERVE > run_limit:
        raise ArtifactLimitExceededError(
            "native evidence exceeds cumulative run budget"
        )
    directory = root / "native"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=directory)
    temporary = Path(temporary_name)
    target = directory / name
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)  # fails if already published; never overwrites
    finally:
        temporary.unlink(missing_ok=True)
    enforce_limit(run_root, run_limit)
    return {**describe_artifact(target), "path": str(target.relative_to(root))}


def persist_native(
    root: Path, checkpoint: str, response: dict, artifact_limit_bytes: int
) -> dict:
    """Publish every available raw source once and return the checkpoint block."""
    if checkpoint not in CHECKPOINTS:
        raise ValueError("unknown memory checkpoint")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    sources = {}
    errors = response.get("errors") or {}
    for key, (suffix, maximum) in _RAW.items():
        source = {
            "interval": (response.get("intervals") or {}).get(key),
            "completion": (response.get("completion") or {}).get(key),
            "available": response.get(key) is not None and key not in errors,
        }
        if key in errors:
            source["error"] = errors[key]
        text = response.get(key)
        if text is not None:
            body = text.encode("utf-8")
            if len(body) > maximum:
                raise ValueError(f"{key} violates its raw evidence bound")
            source["artifact"] = _write_raw(
                root,
                f"{checkpoint}-{suffix}",
                body,
                artifact_limit_bytes,
            )
        sources[key] = source
    block = {
        **summarize(response),
        "sources": sources,
        "phase": CHECKPOINTS[checkpoint],
        "collection": {
            key: response[key]
            for key in ("target", "before", "after", "started_s", "ended_s")
            if key in response
        },
    }
    # Leave room in the existing 1 MiB runtime JSON record for ordinary
    # observations. Full raw mappings stay available even if the summary is huge.
    if len(json.dumps(block).encode("utf-8")) > MAX_RECORD_BYTES // 2:
        block["smaps"] = {
            "available": False,
            "error": (
                "parsed smaps summary exceeds checkpoint record budget; "
                "see raw artifact"
            ),
        }
    return block


def native_comparison(root: Path) -> dict:
    """Return one entry per checkpoint, marking a missing or unreadable one.

    The report entry is a comparison, not a mapping dump: the per-mapping
    records stay in the checkpoint record and its raw artifact, so three
    embedded blocks cannot exceed the report document's own byte budget.
    """
    comparison = {}
    for checkpoint, phase in CHECKPOINTS.items():
        entry = {"phase": phase, "available": False}
        try:
            document = json.loads((root / f"runtime-{checkpoint}.json").read_text())
            block = document["native"]
            if not isinstance(block, dict):
                raise ValueError("native checkpoint block is not an object")
        except (OSError, ValueError, TypeError, KeyError) as error:
            entry["error"] = f"{type(error).__name__}: {error}"[:1024]
        else:
            smaps = block.get("smaps")
            if isinstance(smaps, dict):
                smaps.pop("mapping_details", None)
                large = smaps.get("large_anonymous_mappings")
                if isinstance(large, dict) and "mappings" in large:
                    large["mappings"] = f"see evidence/runtime-{checkpoint}.json"
            entry.update(available=True, native=block)
        comparison[checkpoint] = entry
    return comparison
