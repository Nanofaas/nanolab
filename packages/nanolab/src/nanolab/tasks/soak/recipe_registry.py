"""Verify attested single-platform recipe artifacts without local image IDs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from nanolab.tasks.recipe import _object
from nanolab.tasks.recipe_multiarch import (
    MultiarchDistribution,
    sha256_digest,
    unique_json_object,
)
from nanolab.tasks.recipe_registry import (
    INDEX_TYPES,
    MANIFEST_TYPES,
    RegistryFetch,
    _baseline_platform,
)
from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.build_provenance import _materials, _predicate


@dataclass(frozen=True, slots=True)
class VerifiedSoakImage:
    """Distinct publication, executable and configuration identities."""

    publication_digest: str
    manifest_digest: str
    config_digest: str
    provenance: tuple[dict[str, object], ...]


def verify_soak_registry(
    distribution: MultiarchDistribution,
    *,
    platform: str,
    evidence_dir: Path | None,
    fetch: RegistryFetch,
    artifact_limit_bytes: int = 16 * 1024 * 1024,
    budget_root: Path | None = None,
) -> dict[str, VerifiedSoakImage]:
    """Verify registry bytes, subjects and maximum provenance before deployment."""
    writer = (
        ArtifactWriter(evidence_dir, artifact_limit_bytes, budget_root=budget_root)
        if evidence_dir is not None
        else None
    )
    verified = {}
    try:
        for component in distribution.components:
            image = component.image
            prefix = "127.0.0.1:5000/"
            if (
                not image.reference.startswith(prefix)
                or image.platforms != (platform,)
                or image.provenance is not True
                or image.status != "published"
            ):
                raise ValueError("Registry publication differs from soak platform")
            repository, tag = image.reference[len(prefix) :].rsplit(":", 1)
            role = (
                "control-plane"
                if component.kind == "control-plane"
                else f"{component.name}-{component.sdk}"
            )
            if role in verified or not re.fullmatch(r"[a-zA-Z0-9_-]+", role):
                raise ValueError("Registry component evidence identity is invalid")

            def document(
                kind: str,
                reference: str,
                expected: str,
                name: str,
                descriptor: dict[str, object] | None = None,
                *,
                repository: str = repository,
                role: str = role,
            ):
                response = fetch(repository, kind, reference)
                observed = "sha256:" + hashlib.sha256(response.body).hexdigest()
                if observed != sha256_digest(expected) or (
                    response.digest is not None
                    and sha256_digest(response.digest) != expected
                ):
                    raise ValueError(f"Registry digest mismatch for {name}")
                if descriptor is not None:
                    size = descriptor.get("size")
                    if type(size) is not int or size < 0 or len(response.body) != size:
                        raise ValueError(
                            f"Registry descriptor size mismatch for {name}"
                        )
                if writer is not None:
                    writer.write_blob(role, name, response.body)
                try:
                    return _object(
                        json.loads(response.body, object_pairs_hook=unique_json_object),
                        name,
                    )
                except json.JSONDecodeError as error:
                    raise ValueError(f"Invalid registry JSON for {name}") from error

            document("manifests", tag, image.digest, "tag-before.json")
            index = document("manifests", image.digest, image.digest, "index.json")
            if (
                index.get("schemaVersion") != 2
                or index.get("mediaType") not in INDEX_TYPES
            ):
                raise ValueError("Registry publication requires an attested index")
            descriptors = index.get("manifests")
            if not isinstance(descriptors, list) or len(descriptors) != 2:
                raise ValueError(
                    "Registry index requires one image and one attestation"
                )
            executable = []
            attestations = []
            for raw in descriptors:
                descriptor = _object(raw, "descriptor")
                if descriptor.get("mediaType") not in MANIFEST_TYPES:
                    raise ValueError(
                        "Registry manifest descriptor media type is invalid"
                    )
                annotations = _object(descriptor.get("annotations", {}), "annotations")
                if (
                    annotations.get("vnd.docker.reference.type")
                    == "attestation-manifest"
                ):
                    attestations.append(descriptor)
                else:
                    executable.append(descriptor)
            if len(executable) != 1 or len(attestations) != 1:
                raise ValueError("Registry executable or attestation is ambiguous")
            descriptor = executable[0]
            digest = sha256_digest(descriptor.get("digest"))
            if _baseline_platform(
                _object(descriptor.get("platform"), "platform")
            ) != platform or image.manifests != {platform: digest}:
                raise ValueError("Registry executable platform differs from report")
            manifest = document(
                "manifests", digest, digest, "manifest.json", descriptor
            )
            if (
                manifest.get("schemaVersion") != 2
                or manifest.get("mediaType") not in MANIFEST_TYPES
            ):
                raise ValueError("Registry executable manifest is invalid")
            config_descriptor = _object(manifest.get("config"), "config descriptor")
            config_digest = sha256_digest(config_descriptor.get("digest"))
            config = document(
                "blobs", config_digest, config_digest, "config.json", config_descriptor
            )
            if _baseline_platform(config) != platform:
                raise ValueError("Registry configuration platform differs from image")
            attestation_descriptor = attestations[0]
            if (
                _object(attestation_descriptor.get("annotations"), "annotations").get(
                    "vnd.docker.reference.digest"
                )
                != digest
            ):
                raise ValueError("Registry attestation is bound to a different image")
            attestation_digest = sha256_digest(attestation_descriptor.get("digest"))
            attestation = document(
                "manifests",
                attestation_digest,
                attestation_digest,
                "attestation.json",
                attestation_descriptor,
            )
            if (
                attestation.get("schemaVersion") != 2
                or attestation.get("mediaType") not in MANIFEST_TYPES
            ):
                raise ValueError("Registry attestation manifest is invalid")
            layers = attestation.get("layers")
            if not isinstance(layers, list) or len(layers) != 1:
                raise ValueError(
                    "Registry attestation requires one provenance statement"
                )
            layer = _object(layers[0], "statement descriptor")
            if layer.get("mediaType") != "application/vnd.in-toto+json":
                raise ValueError("Registry provenance statement media type is invalid")
            statement_digest = sha256_digest(layer.get("digest"))
            statement = document(
                "blobs", statement_digest, statement_digest, "statement-0.json", layer
            )
            subjects = statement.get("subject")
            if (
                statement.get("_type")
                not in {
                    "https://in-toto.io/Statement/v0.1",
                    "https://in-toto.io/Statement/v1",
                }
                or not isinstance(statement.get("predicate"), dict)
                or not isinstance(subjects, list)
                or len(subjects) != 1
                or _object(
                    _object(subjects[0], "provenance subject").get("digest"),
                    "provenance subject digest",
                )
                != {"sha256": digest.removeprefix("sha256:")}
            ):
                raise ValueError("Registry provenance statement subject is invalid")
            predicate = _predicate(statement, platform, digest)
            _materials(predicate)
            if not isinstance(predicate.get("buildConfig"), dict) or not predicate[
                "buildConfig"
            ].get("llbDefinition"):
                raise ValueError("Maximum provenance build configuration is missing")
            document("manifests", tag, image.digest, "tag-after.json")
            verified[role] = VerifiedSoakImage(
                image.digest, digest, config_digest, (statement,)
            )
        return verified
    finally:
        if writer is not None:
            writer.close()
