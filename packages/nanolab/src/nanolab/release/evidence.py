"""Fail-closed Sonata evidence verifiers for release artifacts."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from sonata_engine import Evidence, Verifier
from sonata_tasks.cosign import COSIGN_IMAGE

from nanolab.release.build import _provider_exec, _remote_image_digest
from nanolab.release.model import ArtifactEvidence, CredentialFiles, digest_path
from nanolab.release.publish import ghcr_username
from nanolab.release.secrets import stage_cosign_credentials, stage_ghcr_credentials

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
DigestReader = Callable[[str], str | None]

# Every evidence kind a release receipt may carry. A receipt entry outside this
# set is a schema violation, not something to skip over: the whole point of the
# parser is that an unrecognised claim fails the read instead of passing
# unread. `release_evidence_verifiers` must offer a verifier for each one, or a
# phase carrying that kind can never be skipped on resume.
RECEIPT_KINDS = frozenset(
    {
        "file-digest",
        "local-image-digest",
        "local-registry-digest",
        "ghcr-digest",
        "cosign-attestation",
    }
)


def is_sha256_digest(value: str | None) -> bool:
    """Return whether `value` is a lowercase sha256 digest string."""
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def receipt_artifacts(
    path: Path, phase: str, expected_kind: str
) -> tuple[ArtifactEvidence, ...]:
    """Parse one release receipt with a single fail-closed schema.

    A phase may record more than one kind of claim -- `attest` records the
    predicate digest *and* one signature per pinned image -- so entries of
    another kind are filtered out rather than rejected. Filtering is only safe
    because every kind must still be one this module recognises: an entry with
    an unknown kind fails the read, so nothing gets silently skipped.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {phase} receipt") from error
    if (
        not isinstance(payload, Mapping)
        or set(payload) - {"phase", "execution", "evidence"}
        or payload.get("phase") != phase
        or ("execution" in payload and not isinstance(payload["execution"], str))
        or not isinstance(payload.get("evidence"), list)
    ):
        raise ValueError(f"invalid {phase} receipt")
    evidence = payload["evidence"]
    if any(
        not isinstance(item, Mapping)
        or set(item) != {"kind", "reference", "digest"}
        or item.get("kind") not in RECEIPT_KINDS
        or not isinstance(item.get("reference"), str)
        or not isinstance(item.get("digest"), str)
        for item in evidence
    ):
        raise ValueError(f"invalid {phase} receipt")
    location = "remote" if expected_kind == "local-registry-digest" else "local"
    return tuple(
        ArtifactEvidence(location, item["reference"], item["digest"])
        for item in evidence
        if item["kind"] == expected_kind
    )


def file_digest_verifier(evidence: Evidence) -> bool:
    """Verify a regular file without raising on missing or unreadable paths."""
    try:
        return (
            is_sha256_digest(evidence.digest)
            and Path(evidence.reference).is_file()
            and digest_path(Path(evidence.reference)) == evidence.digest
        )
    except OSError:
        return False


def image_digest_verifier(read_digest: DigestReader) -> Verifier:
    """Build a verifier around an injected registry/daemon digest lookup."""

    def verify(evidence: Evidence) -> bool:
        if not is_sha256_digest(evidence.digest):
            return False
        try:
            current = read_digest(evidence.reference)
        except Exception:
            return False
        return current == evidence.digest and current is not None

    return verify


def signature_evidence_verifier(
    evidence: Evidence, *, verify_signature: Callable[[str], bool] | None = None
) -> bool:
    """Require a pinned digest and cryptographic verification under release policy.

    A self-consistent reference is only a claim. Without an authenticated
    verifier, Sonata must rerun the signing phase rather than reuse that claim.
    """
    reference, _, pinned = evidence.reference.partition("@")
    if not (
        evidence.kind == "cosign-attestation"
        and reference.startswith("ghcr.io/")
        and pinned == evidence.digest
        and is_sha256_digest(evidence.digest)
        and verify_signature is not None
    ):
        return False
    try:
        return verify_signature(evidence.reference) is True
    except Exception:
        return False


def authenticated_signature_verifier(
    provider: object, request: object, credentials: CredentialFiles | None
) -> Verifier:
    """Verify both Cosign artifacts against the requested release signing key.

    Credentials are staged only while inspecting evidence. Derive the public
    key afresh, so a leftover VM key cannot select the trust policy on resume.
    Verification uses the same pinned tool and custom predicate type as signing.
    Missing credentials, inaccessible artifacts or failed verification fail closed.
    """

    def verify(reference: str) -> bool:
        if credentials is None or credentials.cosign_password is None:
            return False
        with (
            stage_ghcr_credentials(
                provider,
                request,
                username=ghcr_username(),
                token_file=credentials.ghcr_token,
            ) as docker,
            stage_cosign_credentials(
                provider,
                request,
                key_file=credentials.cosign_key,
                password_file=credentials.cosign_password,
            ) as signing,
        ):
            public_key = f"{signing.key_file}.pub"
            _provider_exec(
                provider,
                request,
                (
                    "sh",
                    "-c",
                    'pw=$(cat "$1") || exit; out="$2"; shift 2; '
                    'COSIGN_PASSWORD="$pw" "$@" > "$out" && test -s "$out"',
                    "--",
                    str(signing.password_file),
                    public_key,
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    "0",
                    "-e",
                    "COSIGN_PASSWORD",
                    "-v",
                    f"{signing.key_file}:/key.cosign:ro",
                    COSIGN_IMAGE,
                    "public-key",
                    "--key",
                    "/key.cosign",
                ),
            )
            base = (
                "docker",
                "run",
                "--rm",
                "--user",
                "0",
                "-e",
                "DOCKER_CONFIG=/auth",
                "-v",
                f"{docker.docker_config}:/auth:ro",
                "-v",
                f"{public_key}:/pub.key:ro",
                COSIGN_IMAGE,
            )
            for operation in (
                ("verify", "--key", "/pub.key", reference),
                (
                    "verify-attestation",
                    "--key",
                    "/pub.key",
                    "--type",
                    "custom",
                    reference,
                ),
            ):
                _provider_exec(provider, request, (*base, *operation), bounded=True)
        return True

    return lambda evidence: signature_evidence_verifier(
        evidence, verify_signature=verify
    )


def release_evidence_verifiers(
    provider: object,
    request: object,
    *,
    ghcr_authfile: str | None = None,
    credentials: CredentialFiles | None = None,
) -> dict[str, Verifier]:
    """Return release verifiers; GHCR fails closed until auth is staged."""

    def remote(reference: str, *, authfile: str | None = None) -> str | None:
        return _remote_image_digest(
            provider,
            request,
            "remote",
            reference,
            ghcr_authfile=authfile,
        )

    return {
        "file-digest": file_digest_verifier,
        "cosign-attestation": authenticated_signature_verifier(
            provider, request, credentials
        ),
        "local-image-digest": image_digest_verifier(
            lambda reference: (
                remote(reference) if reference.startswith("docker-daemon:") else None
            )
        ),
        "local-registry-digest": image_digest_verifier(
            lambda reference: (
                remote(reference)
                if reference.startswith("docker://")
                and not reference.startswith("docker://ghcr.io/")
                else None
            )
        ),
        "ghcr-digest": image_digest_verifier(
            lambda reference: (
                remote(reference, authfile=ghcr_authfile)
                if ghcr_authfile is not None
                and reference.startswith("docker://ghcr.io/")
                else None
            )
        ),
    }
