"""Identify and stage local build inputs without weakening release source guards."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from nanolab.tasks.soak.artifacts import ArtifactWriter, describe_artifact, fingerprint

_MAX_FILES = 100000


class SourceChangedError(ValueError):
    """Source inputs or the identified snapshot changed across a build boundary."""


@dataclass(frozen=True)
class SourceEntry:
    """One source path, its content identity and build-relevant filesystem mode."""

    path: str
    kind: str
    mode: int
    size_bytes: int
    sha256: str
    link_target: str | None = None


@dataclass(frozen=True)
class SourceSnapshot:
    """An identified plain source tree, distinct from mutable build workspaces."""

    root: Path
    fingerprint: str
    revision: str | None
    dirty: bool
    entries: tuple[SourceEntry, ...]
    manifest_path: Path
    manifest_sha256: str


def _git(root: Path, *args: str, optional: bool = False) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(root), *args), capture_output=True, check=False, timeout=30
    )
    if result.returncode:
        if optional:
            return b""
        raise ValueError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _entry(root: Path, name: str, paths: set[str]) -> SourceEntry:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
        raise ValueError(f"unsafe source input path: {name}")
    path = root / relative
    if path.is_symlink():
        target = str(path.readlink())
        resolved = path.resolve()
        if Path(target).is_absolute() or not resolved.is_relative_to(root):
            raise ValueError(f"source symlink escapes the snapshot: {name}")
        resolved_name = resolved.relative_to(root).as_posix()
        included = resolved_name in paths or any(
            item.startswith(resolved_name + "/") for item in paths
        )
        try:
            resolved.stat()
        except FileNotFoundError:
            # Preserve an already-dangling internal link verbatim. Only ENOENT
            # means absence; access errors, cycles and invalid paths still fail.
            pass
        else:
            if not included:
                raise ValueError(f"source symlink target is not captured: {name}")
        encoded = os.fsencode(target)
        return SourceEntry(
            name,
            "symlink",
            stat.S_IMODE(path.lstat().st_mode),
            len(encoded),
            hashlib.sha256(encoded).hexdigest(),
            target,
        )
    if not path.exists():
        return SourceEntry(name, "deleted", 0, 0, "")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError(f"unsupported source input: {name}")
    identity = describe_artifact(path)
    return SourceEntry(
        name,
        "file",
        stat.S_IMODE(path.stat().st_mode),
        int(str(identity["size_bytes"])),
        str(identity["sha256"]),
    )


def _inventory(root: Path, max_bytes: int) -> tuple[SourceEntry, ...]:
    stages = _git(root, "ls-files", "--stage", "-z").split(b"\0")
    if any(record.startswith(b"160000 ") for record in stages):
        raise ValueError(
            "submodules require an explicitly supported source snapshot recipe"
        )
    paths = {
        os.fsdecode(item)
        for item in _git(
            root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
        ).split(b"\0")
        if item
    }
    if len(paths) > _MAX_FILES:
        raise ValueError("source snapshot exceeds the input file budget")
    entries = []
    size = 0
    for path in sorted(paths):
        entry = _entry(root, path, paths)
        size += entry.size_bytes
        if size > max_bytes:
            raise ValueError("source snapshot exceeds the byte budget")
        entries.append(entry)
    return tuple(entries)


def _tree_entries(root: Path) -> tuple[SourceEntry, ...]:
    paths: set[str] = set()
    for parent, directories, files in os.walk(root, followlinks=False):
        directory = Path(parent)
        for name in directories:
            path = directory / name
            if path.is_symlink():
                paths.add(path.relative_to(root).as_posix())
        for name in files:
            paths.add((directory / name).relative_to(root).as_posix())
        if len(paths) > _MAX_FILES:
            raise SourceChangedError("snapshot tree exceeds the input file budget")
    return tuple(_entry(root, name, paths) for name in sorted(paths))


def capture_source_snapshot(
    repo_root: Path, destination: Path, *, max_bytes: int
) -> SourceSnapshot:
    """Copy tracked and nonignored local inputs, rejecting concurrent source changes."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("source byte budget must be a positive integer")
    root = repo_root.resolve()
    output = destination.absolute()
    if output.resolve().is_relative_to(root):
        raise ValueError("snapshot destination must be outside the source checkout")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"snapshot destination already exists: {output}")
    top = Path(
        os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()
    ).resolve()
    if top != root:
        raise ValueError("source must name the repository root")
    revision = (
        _git(root, "rev-parse", "--verify", "HEAD", optional=True).decode().strip()
        or None
    )
    state = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = _inventory(root, max_bytes)
    digest = fingerprint({"entries": [asdict(entry) for entry in entries]})
    output.mkdir(parents=True, exist_ok=False)
    writer = ArtifactWriter(output, limit_bytes=16 * 1024 * 1024)
    tree = output / "tree"
    tree.mkdir()
    try:
        for entry in entries:
            target = tree / entry.path
            if entry.kind != "deleted":
                target.parent.mkdir(parents=True, exist_ok=True)
            if entry.kind == "file":
                shutil.copyfile(root / entry.path, target, follow_symlinks=False)
                target.chmod(entry.mode)
            elif entry.kind == "symlink":
                if entry.link_target is None:
                    raise ValueError("symlink source entry is missing its target")
                target.symlink_to(entry.link_target)
            writer.append("source-manifest", asdict(entry))
        after_revision = (
            _git(root, "rev-parse", "--verify", "HEAD", optional=True).decode().strip()
            or None
        )
        after_state = _git(
            root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        if (
            revision != after_revision
            or state != after_state
            or entries != _inventory(root, max_bytes)
        ):
            raise SourceChangedError("source changed while creating its snapshot")
        expected = tuple(entry for entry in entries if entry.kind != "deleted")
        if _tree_entries(tree) != expected:
            raise SourceChangedError("copied source does not match its input manifest")
        manifest = output / "source-manifest.jsonl"
        # An empty repository still has a complete manifest to identify.
        if not entries:
            manifest.touch(exist_ok=False)
        snapshot = SourceSnapshot(
            tree,
            digest,
            revision,
            bool(state),
            entries,
            manifest,
            str(describe_artifact(manifest)["sha256"]),
        )
        writer.write_json(
            "snapshot.json",
            {
                "schema": "nanolab-soak-v1",
                "status": "captured",
                "source_root": str(root),
                "root": str(tree),
                "revision": revision,
                "dirty": snapshot.dirty,
                "fingerprint": digest,
                "manifest_sha256": snapshot.manifest_sha256,
                "entry_count": len(entries),
            },
        )
        return snapshot
    finally:
        writer.close()


def verify_snapshot(snapshot: SourceSnapshot) -> None:
    """Reject a changed manifest or source tree before a build consumes it."""
    expected = tuple(entry for entry in snapshot.entries if entry.kind != "deleted")
    if (
        not snapshot.root.is_dir()
        or _tree_entries(snapshot.root) != expected
        or fingerprint({"entries": [asdict(entry) for entry in snapshot.entries]})
        != snapshot.fingerprint
        or str(describe_artifact(snapshot.manifest_path)["sha256"])
        != snapshot.manifest_sha256
    ):
        raise SourceChangedError("snapshot no longer matches its frozen identity")


def materialize_snapshot(snapshot: SourceSnapshot, destination: Path) -> Path:
    """Create an independent workspace; never compile inside the snapshot."""
    verify_snapshot(snapshot)
    output = destination.absolute()
    if output.resolve().is_relative_to(snapshot.root.resolve()):
        raise ValueError("build workspace must be outside the frozen source tree")
    shutil.copytree(snapshot.root, output, symlinks=True)
    expected = tuple(entry for entry in snapshot.entries if entry.kind != "deleted")
    if _tree_entries(output) != expected:
        raise SourceChangedError("build workspace does not match the frozen source")
    verify_snapshot(snapshot)
    return output
