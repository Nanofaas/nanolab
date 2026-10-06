"""Verify immutable registry bytes for multiarch recipe publication."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from nanolab.tasks.recipes.multiarch import (
    MultiarchDistribution,
    sha256_digest,
    unique_json_object,
)
from nanolab.tasks.recipes.workflow import _object

INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}


@dataclass(frozen=True, slots=True)
class RegistryResponse:
    """Registry response preserving the bytes whose digest identifies it."""

    body: bytes
    digest: str | None
    media_type: str


RegistryFetch = Callable[[str, str, str], RegistryResponse]


def _baseline_platform(data: dict[str, object]) -> str:
    architecture = data.get("architecture")
    aliases = {"amd64": (None, "", "v1"), "arm64": (None, "", "v8")}
    if (
        not isinstance(architecture, str)
        or architecture not in aliases
        or data.get("variant") not in aliases[architecture]
    ):
        raise ValueError(
            "Registry platform variant is incompatible with baseline recipe"
        )
    return f"{data.get('os')}/{architecture}"


def fetch_local_registry(
    repository: str, kind: str, reference: str
) -> RegistryResponse:
    """Fetch only from the run's local registry without redirect or proxy escape."""
    if (
        not re.fullmatch(r"nanofaas/[a-z0-9]+(?:[._/-][a-z0-9]+)*", repository)
        or kind not in {"manifests", "blobs"}
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}|[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", reference
        )
    ):
        raise ValueError("Invalid local registry object")
    url = f"http://127.0.0.1:5000/v2/{repository}/{kind}/{reference}"
    try:
        with httpx.Client(
            timeout=30, follow_redirects=False, trust_env=False
        ) as client:
            result = client.get(
                url,
                headers={
                    "Accept": ", ".join(sorted(INDEX_TYPES | MANIFEST_TYPES)),
                    "Accept-Encoding": "identity",
                },
            )
            result.raise_for_status()
    except httpx.HTTPError as error:
        raise ValueError(f"Cannot fetch registry {kind}/{reference}") from error
    return RegistryResponse(
        result.content,
        result.headers.get("Docker-Content-Digest"),
        result.headers.get("Content-Type", ""),
    )


def verify_multiarch_registry(
    distribution: MultiarchDistribution, *, fetch: RegistryFetch, evidence_dir: Path
) -> dict[str, dict[str, str]]:
    """Verify both executable platforms, retaining index/manifest/config bytes."""
    verified: dict[str, dict[str, str]] = {}
    for component in distribution.components:
        image = component.image
        prefix = "127.0.0.1:5000/"
        if not image.reference.startswith(prefix):
            raise ValueError("Multiarch image must use the local registry")
        repository, tag = image.reference[len(prefix) :].rsplit(":", 1)
        if any(
            not re.fullmatch(r"[a-zA-Z0-9_-]+", value)
            for value in (component.kind, component.name, component.sdk)
        ):
            raise ValueError("Invalid component evidence path")
        directory = evidence_dir / f"{component.kind}-{component.name}-{component.sdk}"
        directory.mkdir(parents=True, exist_ok=True)

        def document(
            kind: str,
            reference: str,
            expected: str,
            filename: str,
            size: object = None,
            *,
            repository: str = repository,
            directory: Path = directory,
        ):
            response = fetch(repository, kind, reference)
            (directory / filename).write_bytes(response.body)
            observed = "sha256:" + hashlib.sha256(response.body).hexdigest()
            if observed != expected or (
                response.digest is not None
                and sha256_digest(response.digest) != expected
            ):
                raise ValueError(f"Registry digest mismatch for {filename}")
            if size is not None and (
                type(size) is not int or size < 0 or len(response.body) != size
            ):
                raise ValueError(f"Registry descriptor size mismatch for {filename}")
            try:
                return _object(
                    json.loads(response.body, object_pairs_hook=unique_json_object),
                    filename,
                )
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid registry JSON for {filename}") from error

        document("manifests", tag, image.digest, "tag-before.json")
        index = document("manifests", image.digest, image.digest, "index.json")
        if index.get("schemaVersion") != 2 or index.get("mediaType") not in INDEX_TYPES:
            raise ValueError("Registry image is not a supported index manifest")
        descriptors = index.get("manifests")
        if not isinstance(descriptors, list) or len(descriptors) != len(
            image.platforms
        ):
            raise ValueError("Registry manifest platform count differs from recipe")
        configs: dict[str, str] = {}
        for raw_descriptor in descriptors:
            descriptor = _object(raw_descriptor, "manifest descriptor")
            annotations = _object(descriptor.get("annotations", {}), "annotations")
            if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
                raise ValueError("Unexpected attestation manifest")
            platform_data = _object(descriptor.get("platform"), "platform")
            platform = _baseline_platform(platform_data)
            digest = sha256_digest(descriptor.get("digest"))
            if (
                platform not in image.manifests
                or platform in configs
                or digest != image.manifests[platform]
            ):
                raise ValueError(
                    "Registry manifest platform identity differs from report"
                )
            if descriptor.get("mediaType") not in MANIFEST_TYPES:
                raise ValueError("Registry manifest descriptor is invalid")
            if type(descriptor.get("size")) is not int or descriptor["size"] < 0:
                raise ValueError("Registry manifest descriptor size is invalid")
            slug = platform.replace("/", "-")
            manifest = document(
                "manifests", digest, digest, f"{slug}-manifest.json", descriptor["size"]
            )
            if (
                manifest.get("schemaVersion") != 2
                or manifest.get("mediaType") not in MANIFEST_TYPES
            ):
                raise ValueError("Registry executable manifest is invalid")
            config_descriptor = _object(manifest.get("config"), "config descriptor")
            config_digest = sha256_digest(config_descriptor.get("digest"))
            if (
                type(config_descriptor.get("size")) is not int
                or config_descriptor["size"] < 0
            ):
                raise ValueError("Registry config descriptor size is invalid")
            config = document(
                "blobs",
                config_digest,
                config_digest,
                f"{slug}-config.json",
                config_descriptor["size"],
            )
            if _baseline_platform(config) != platform:
                raise ValueError(
                    "Registry configuration platform differs from manifest"
                )
            configs[platform] = config_digest
        if set(configs) != set(image.platforms):
            raise ValueError("Registry platform selection is incomplete")
        document("manifests", tag, image.digest, "tag-after.json")
        (directory / "verification.json").write_text(
            json.dumps(
                {
                    "reference": image.reference,
                    "indexDigest": image.digest,
                    "manifests": image.manifests,
                    "configs": configs,
                },
                indent=2,
            )
            + "\n"
        )
        verified[image.reference] = configs
    return verified
