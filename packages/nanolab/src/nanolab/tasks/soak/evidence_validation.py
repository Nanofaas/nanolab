"""Verify offline evidence paths, bounded receipts and containerd build identities."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypeGuard

from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.artifacts import (
    describe_tree,
)
from nanolab.tasks.soak.sources import SourceSnapshot

_LIMIT = 1024 * 1024


def _require(condition: object, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _finite(value: object) -> TypeGuard[float]:
    """Report whether this is a finite numeric measurement.

    `type(x) is T` rather than isinstance, because bool is an int subclass and
    a boolean is never a measurement. A TypeGuard so that the callers, which
    all go on to compare or convert the value, are narrowed by the check they
    already perform.
    """
    if type(value) is not int and type(value) is not float:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _count(value: object) -> TypeGuard[int]:
    """Report whether this is a non-negative integer count."""
    return type(value) is int and value >= 0


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("nonfinite JSON value: " + value)


def _path(root: Path, name: str, *, tree: bool = False) -> Path:
    """Resolve one recorded path. `tree` also admits an inventory tree entry."""
    _require(isinstance(name, str), "artifact path must be a string")
    relative = Path(name)
    _require(
        not root.is_symlink()
        and not relative.is_absolute()
        and bool(relative.parts)
        and ".." not in relative.parts,
        "artifact path must remain inside run",
    )
    current = root
    for part in relative.parts:
        current /= part
        _require(not current.is_symlink(), "symlink evidence is unsupported")
    _require(
        current.is_file() or (tree and current.is_dir()),
        "missing regular artifact: " + name,
    )
    return current


def _json(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        body = stream.read(_LIMIT + 1)
    _require(len(body) <= _LIMIT, "receipt exceeds JSON size limit")
    value = json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant)
    _require(isinstance(value, dict), "receipt must be a JSON object")
    return value


def _reference(root: Path, record: dict[str, Any], limit: int) -> Path:
    _require(isinstance(record, dict), "missing artifact reference")
    name = Path(record["path"])
    if name.is_absolute():
        name = name.relative_to(root.absolute())
    path = _path(root, str(name))
    _require(path.stat().st_size <= limit, "artifact exceeds frozen byte budget")
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            _require(size <= limit, "artifact grew beyond frozen byte budget")
            digest.update(chunk)
    _require(record.get("sha256") == digest.hexdigest(), "artifact checksum mismatch")
    if "size_bytes" in record:
        _require(
            _count(record["size_bytes"]) and record["size_bytes"] == size,
            "artifact size mismatch",
        )
    return path


def _inventory_reference(
    root: Path, record: dict[str, Any], limit: int
) -> tuple[Path, int]:
    """Verify one acceptance-inventory entry and charge the bytes it names.

    Almost every entry is one file, verified by the shared `_reference`. A
    helper's command-log tree is a single entry standing for thousands of
    command logs, so it is verified the way the producer measured it: every
    retained file hashed into one digest, and the exact byte total the per-file
    entries carried, which is what the budget was charged.
    """
    name = Path(record["path"])
    if name.is_absolute():
        name = name.relative_to(root.absolute())
    path = _path(root, str(name), tree=True)
    if not path.is_dir():
        return _reference(root, record, limit), path.stat().st_size
    measured = describe_tree(root, path)
    _require(
        record.get("sha256") == measured["sha256"]
        and _count(record.get("size_bytes"))
        and record["size_bytes"] == measured["size_bytes"],
        "artifact tree checksum mismatch",
    )
    return path, measured["size_bytes"]


def verify_containerd_builds(
    root: Path,
    manifest: dict[str, Any],
    config: SoakConfig,
    targets: dict[str, dict[str, Any]],
    source: SourceSnapshot,
    observed: dict[str, Any],
) -> str:
    """Bind staged source, process binary and running OCI tasks to receipts."""

    def repository(reference: str) -> str:
        name = reference.split("@", 1)[0]
        return name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name

    remote = _json(
        _reference(root, manifest["remote_source"], config.artifact_limit_bytes)
    )
    batches = [
        hashlib.sha256(
            json.dumps(
                [asdict(entry) for entry in source.entries[offset : offset + 50]],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for offset in range(0, len(source.entries), 50)
    ]
    _require(
        remote == observed.get("remote_source")
        and remote.get("schema") == "nanolab-containerd-source-v1"
        and remote.get("revision") == source.revision
        and remote.get("source_fingerprint") == source.fingerprint
        and remote.get("clean") is True
        and source.dirty is False
        and remote.get("entry_count") == len(source.entries)
        and remote.get("batch_size") == 50
        and remote.get("batches") == batches
        and remote.get("verification")
        == "remote-content-after-build; rsync excludes .git",
        "staged source differs from frozen local checkout",
    )
    for role, spec in config.images.items():
        build = _json(
            _reference(root, manifest["builds"][role], config.artifact_limit_bytes)
        )
        recipe = _json(
            _reference(root, manifest["recipes"][role], config.artifact_limit_bytes)
        )
        digest = targets[role]["image_digest"]
        if spec.artifact_kind == "process":
            _require(
                role == "control-plane"
                and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None,
                "process artifact digest must be a SHA256 of the running file",
            )
        _require(
            build.get("schema") == "nanolab-containerd-build-v1"
            and build.get("role") == role
            and build.get("image_digest") == digest
            and build.get("source_fingerprint") == source.fingerprint
            and build.get("source_revision") == source.revision
            and build.get("platform") == spec.platform
            and build.get("platform") == observed["roles"][role].get("platform")
            and build.get("artifact_kind") == spec.artifact_kind
            and build.get("artifact_path")
            == observed["roles"][role].get("artifact_path")
            and all(
                observed["build_receipts"][role].get(key) == value
                for key, value in build.items()
                if key != "schema"
            ),
            "running artifact/source/build receipt differs",
        )
        command = build.get("build_argv")
        steps = build.get("build_steps")
        executions = build.get("build_results")
        if not isinstance(command, list) or not isinstance(steps, list):
            raise ValueError("containerd build command differs from frozen recipe")
        _require(
            isinstance(command, list)
            and bool(command)
            and all(isinstance(arg, str) and arg for arg in command)
            and isinstance(steps, list)
            and bool(steps)
            and all(
                isinstance(step, list)
                and bool(step)
                and all(isinstance(arg, str) and arg for arg in step)
                for step in steps
            )
            and steps[-1] == command
            and recipe
            == {
                "role": role,
                "artifact_kind": spec.artifact_kind,
                "platform": spec.platform,
                "build_argv": command,
                "build_steps": steps,
                "mode": spec.mode,
                "variant": spec.variant,
                "modules": spec.modules,
                "build_options": spec.build_options,
            },
            "containerd build command differs from frozen recipe",
        )
        _require(
            all(
                build.get(key) == recipe[key]
                for key in (
                    "build_steps",
                    "mode",
                    "variant",
                    "modules",
                    "build_options",
                )
            )
            and len(steps)
            == (2 if spec.variant == "jvm" and role != "control-plane" else 1),
            "containerd build steps or requested image recipe differ",
        )
        expected_titles = (
            [f"Build application artifact: {role}", f"Build image {role}"]
            if role != "control-plane" and spec.variant == "jvm"
            else [
                "Build control plane"
                if role == "control-plane"
                else f"Build image {role}"
            ]
        )
        _require(
            isinstance(executions, list)
            and executions
            == [
                {"title": title, "argv": step, "status": "passed", "return_code": 0}
                for title, step in zip(expected_titles, steps, strict=True)
            ],
            "containerd build task results differ from executed steps",
        )
        if role != "control-plane" and spec.variant == "jvm":
            _require(
                len(steps[0]) >= 2
                and steps[0][0] == "./gradlew"
                and steps[0][1].startswith(":functions:java:")
                and steps[0][1].endswith(":bootJar"),
                "Java application artifact build is missing",
            )
        if spec.artifact_kind == "process":
            _require(
                isinstance(build.get("artifact_path"), str)
                and Path(build["artifact_path"]).is_absolute()
                and "-PcontrolPlaneModules=" + ",".join(spec.modules) in command,
                "process artifact path/modules differ from policy",
            )
        else:
            _require(
                re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", digest) is not None,
                "function OCI digest missing",
            )
            tag_index = command.index("-t") if "-t" in command else -1
            _require(
                role != "control-plane"
                and tag_index >= 0
                and tag_index + 1 < len(command)
                and repository(command[tag_index + 1]) == repository(digest),
                "function build tag differs from running image repository",
            )
    return "staged source, systemd process artifact and running OCI functions verified"
