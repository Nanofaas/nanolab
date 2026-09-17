"""Build the diagnostic helper image for one run and freeze its digest.

The helper is built here rather than pinned in a checked-in scenario. A digest
resolved on one machine names bytes that exist only in that machine's registry:
committing it hands everyone else a reference nothing can pull, and a prune
makes it unresolvable even locally. Building per run keeps the guarantee that
actually matters for comparing two dumps - that every capture and the analysis
share one immutable image *within* the run - while the inputs stay pinned in
`assets/soak/mat.lock.json` and `assets/soak/helper-bases.lock.json`.

The registry is not a new requirement: preparation already pushes every
application image to it and refuses to start when it does not answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from nanolab.tasks.soak.processes import run_owned_command

_DIGEST = re.compile(r"[^\s@]+@sha256:[a-f0-9]{64}")
# The helper is built from this package, resolved from this file rather than
# from a caller-supplied checkout root. nanolab once lived at
# `packages/nanolab` *inside* the nanoFaaS checkout, where a repo_root-relative
# lookup was correct; after the extraction to a standalone workspace that path
# exists only under nanolab's own root, and nothing noticed until a real `run`
# tried to build. Same derivation the asset lookup below already uses.
BUILD_CONTEXT = Path(__file__).resolve().parents[4]
_ASSETS = BUILD_CONTEXT / "assets" / "soak"
MAT_LOCK = _ASSETS / "mat.lock.json"
BASES_LOCK = _ASSETS / "helper-bases.lock.json"
DOCKERFILE = "assets/soak/diagnostic-helper.Dockerfile"
_LOG_BYTES = 4 * 1024 * 1024


class HelperImageError(RuntimeError):
    """The helper image could not be built, published or resolved."""


@dataclass(frozen=True)
class HelperImageRequest:
    """One run's helper build: where to publish it and what to build it from."""

    run_dir: Path
    run_id: str
    registry: str
    builder: str
    image_name: str = "diagnostic-helper"
    timeout_s: float = 1800.0
    docker: str = "/usr/bin/docker"

    def __post_init__(self) -> None:
        """Reject a registry URL, an unusable run id, or a missing context."""
        if not self.run_id or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.run_id
        ):
            raise ValueError("run_id must be a tag-safe identifier")
        if not self.registry or re.search(r"\s|@|://", self.registry):
            raise ValueError("registry must be an image repository prefix, not a URL")
        if not (BUILD_CONTEXT / DOCKERFILE).is_file():
            raise ValueError(f"helper Dockerfile is missing under {BUILD_CONTEXT}")
        if not self.run_dir.is_dir():
            raise ValueError("run_dir must be an existing directory")

    @property
    def reference(self) -> str:
        """The mutable tag this build publishes; only its digest is ever used."""
        return f"{self.registry.rstrip('/')}/{self.image_name}:{self.run_id}"


def _lock(path: Path, *keys: str) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HelperImageError(
            f"unreadable helper lock file {path}: {error}"
        ) from error
    if not isinstance(document, dict) or any(key not in document for key in keys):
        raise HelperImageError(f"helper lock file {path} is missing {keys}")
    return document


def build_arguments() -> dict[str, str]:
    """Read the pinned MAT archive and base images the helper is built from."""
    mat = _lock(MAT_LOCK, "url", "sha256")
    bases = _lock(BASES_LOCK, "jdk_base", "python_base")
    arguments: dict[str, str] = {
        "MAT_URL": str(mat["url"]),
        "MAT_SHA256": str(mat["sha256"]),
    }
    for name, key in (("JDK_BASE", "jdk_base"), ("PYTHON_BASE", "python_base")):
        entry = bases[key]
        reference = entry.get("reference") if isinstance(entry, dict) else None
        if not isinstance(reference, str) or _DIGEST.fullmatch(reference) is None:
            raise HelperImageError(f"{key} must be a digest-pinned base image")
        arguments[name] = reference
    return arguments


def _resolve(metadata: Path, reference: str) -> str:
    try:
        document = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HelperImageError(f"unreadable build metadata: {error}") from error
    digest = (
        document.get("containerimage.digest") if isinstance(document, dict) else None
    )
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", digest) is None
    ):
        raise HelperImageError("the build published no image digest")
    resolved = f"{reference.rsplit(':', 1)[0]}@{digest}"
    if _DIGEST.fullmatch(resolved) is None:
        raise HelperImageError(
            f"resolved helper reference is not digest-pinned: {resolved}"
        )
    return resolved


def build_helper_image(
    request: HelperImageRequest, *, cancelled: Event | None = None
) -> str:
    """Build and publish the helper, returning its immutable RepoDigest.

    The digest comes from the build's own metadata rather than a later registry
    query, so the reference names exactly the bytes this build published.
    """
    arguments = build_arguments()
    metadata = (request.run_dir / "helper-image-metadata.json").absolute()
    argv = [
        request.docker,
        "buildx",
        "build",
        "--builder",
        request.builder,
        "--platform",
        "linux/arm64",
        "--file",
        DOCKERFILE,
        "--tag",
        request.reference,
        "--push",
        "--provenance=mode=max",
        "--metadata-file",
        str(metadata),
        "--progress=plain",
    ]
    for name, value in arguments.items():
        argv += ["--build-arg", f"{name}={value}"]
    argv.append(".")
    result = run_owned_command(
        argv,
        cwd=BUILD_CONTEXT,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "DOCKER_BUILDKIT": "1"},
        log_path=(request.run_dir / "helper-image-build.log").absolute(),
        timeout_s=request.timeout_s,
        cancelled=cancelled or Event(),
        output_limit_bytes=_LOG_BYTES,
    )
    if (
        result.returncode != 0
        or not result.reaped
        or result.errors
        or result.cancelled
        or result.timed_out
        or result.forced_stop
        or result.quota_exceeded
    ):
        raise HelperImageError(
            "helper image build did not complete cleanly; see "
            f"helper-image-build.log (exit {result.returncode})"
        )
    return _resolve(metadata, request.reference)
