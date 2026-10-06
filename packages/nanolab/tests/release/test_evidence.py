from pathlib import Path

from sonata_engine import Evidence

from nanolab.release.evidence import (
    file_digest_verifier,
    image_digest_verifier,
    release_evidence_verifiers,
    signature_evidence_verifier,
)
from nanolab.release.model import digest_path


def test_file_digest_verifier_fails_closed(tmp_path: Path) -> None:
    artifact = tmp_path / "receipt.json"
    artifact.write_text("original", encoding="utf-8")
    evidence = Evidence("file-digest", str(artifact), digest_path(artifact))

    assert file_digest_verifier(evidence)
    artifact.write_text("changed", encoding="utf-8")
    assert not file_digest_verifier(evidence)
    artifact.unlink()
    assert not file_digest_verifier(evidence)


def test_image_digest_verifier_fails_closed() -> None:
    digest = "sha256:" + "a" * 64
    evidence = Evidence(
        "local-registry-digest", "docker://localhost:5000/image:v1", digest
    )

    assert image_digest_verifier(lambda _reference: digest)(evidence)
    assert not image_digest_verifier(lambda _reference: "sha256:" + "c" * 64)(evidence)
    assert not image_digest_verifier(lambda _reference: "not-a-digest")(evidence)
    assert not image_digest_verifier(lambda _reference: None)(evidence)

    def unreachable(_reference: str) -> str:
        raise OSError("registry unavailable")

    assert not image_digest_verifier(unreachable)(evidence)


def test_signature_verifier_fails_closed_without_crypto_verification() -> None:
    """A signature claim must name the digest it says it signed.

    Sonata skips a resumed phase when this returns True, so it must reject a
    claim that could belong to some other artifact, and a claim whose signature
    cannot be verified.
    """
    digest = "sha256:" + "a" * 64
    reference = f"ghcr.io/nanofaas/gateway@{digest}"

    assert not signature_evidence_verifier(
        Evidence("cosign-attestation", reference, digest)
    )
    # a tag, not a digest: cosign would sign whatever it points at today
    assert not signature_evidence_verifier(
        Evidence("cosign-attestation", "ghcr.io/nanofaas/gateway:v1", digest)
    )
    # pinned to one artifact, claiming the digest of another
    assert not signature_evidence_verifier(
        Evidence("cosign-attestation", reference, "sha256:" + "b" * 64)
    )


def test_authenticated_ghcr_verifier_uses_authfile_without_exposing_token(
    monkeypatch,
) -> None:
    digest = "sha256:" + "b" * 64
    seen: list[tuple[str, str | None]] = []

    def inspect(_provider, _request, _location, reference, *, ghcr_authfile=None):
        seen.append((reference, ghcr_authfile))
        return digest

    monkeypatch.setattr("nanolab.release.evidence._remote_image_digest", inspect)
    token = "fixture-ghcr-token-must-not-leak"
    verifiers = release_evidence_verifiers(
        object(), object(), ghcr_authfile="/staged/auth.json"
    )
    evidence = Evidence("ghcr-digest", "docker://ghcr.io/nanofaas/image:v1", digest)
    assert verifiers["ghcr-digest"](evidence)
    assert seen == [(evidence.reference, "/staged/auth.json")]
    assert token not in repr(verifiers)
    assert verifiers.get("unknown-kind") is None
    assert not release_evidence_verifiers(object(), object())["ghcr-digest"](evidence)


def test_nonexistent_signature_reference_is_not_resume_evidence() -> None:
    digest = "sha256:" + "f" * 64
    assert not signature_evidence_verifier(
        Evidence("cosign-attestation", f"ghcr.io/nonexistent/image@{digest}", digest)
    )


def test_signature_verifier_requires_successful_crypto_policy_check() -> None:
    digest = "sha256:" + "a" * 64
    evidence = Evidence(
        "cosign-attestation", f"ghcr.io/nanofaas/image@{digest}", digest
    )
    assert signature_evidence_verifier(evidence, verify_signature=lambda _ref: True)
    assert not signature_evidence_verifier(
        evidence, verify_signature=lambda _ref: False
    )

    def unavailable(_reference):
        raise OSError("registry unavailable")

    assert not signature_evidence_verifier(evidence, verify_signature=unavailable)


def test_authenticated_signature_verifier_checks_both_artifacts_and_cleans_up(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    from nanolab.release.model import CredentialFiles

    digest = "sha256:" + "a" * 64
    evidence = Evidence(
        "cosign-attestation", f"ghcr.io/nanofaas/image@{digest}", digest
    )
    credentials = CredentialFiles(tmp_path / "token", tmp_path / "key", tmp_path / "pw")
    cleanup = []
    commands = []
    fail: list[str | None] = [None]

    @contextmanager
    def docker_stage(_provider, _request, *, username, token_file):
        assert username == "miciav"
        assert token_file == credentials.ghcr_token
        try:
            yield SimpleNamespace(docker_config="/staged/auth")
        finally:
            cleanup.append("docker")

    @contextmanager
    def signing_stage(_provider, _request, *, key_file, password_file):
        assert (key_file, password_file) == (
            credentials.cosign_key,
            credentials.cosign_password,
        )
        try:
            yield SimpleNamespace(key_file="/staged/key", password_file="/staged/pw")
        finally:
            cleanup.append("signing")

    def execute(_provider, _request, argv, **kwargs):
        commands.append(argv)
        if fail[0] in argv:
            raise RuntimeError("cosign rejected signature or attestation")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("nanolab.release.evidence.stage_ghcr_credentials", docker_stage)
    monkeypatch.setattr(
        "nanolab.release.evidence.stage_cosign_credentials", signing_stage
    )
    monkeypatch.setattr("nanolab.release.evidence._provider_exec", execute)
    verifier = release_evidence_verifiers(object(), object(), credentials=credentials)[
        "cosign-attestation"
    ]
    assert verifier(evidence)
    assert cleanup == ["signing", "docker"]
    assert "public-key" in commands[0]
    assert commands[1][-4:] == ("verify", "--key", "/pub.key", evidence.reference)
    assert commands[2][-6:] == (
        "verify-attestation",
        "--key",
        "/pub.key",
        "--type",
        "custom",
        evidence.reference,
    )
    assert "/staged/auth:/auth:ro" in commands[1]
    assert "/staged/key.pub:/pub.key:ro" in commands[2]
    for operation in ("public-key", "verify", "verify-attestation"):
        commands.clear()
        cleanup.clear()
        fail[0] = operation
        assert not verifier(evidence)
        assert cleanup == ["signing", "docker"]
    assert not release_evidence_verifiers(object(), object())["cosign-attestation"](
        evidence
    )


class DaemonProvider:
    """Two isolated image stores; only the external VM command is substituted."""

    def __init__(self, stores):
        self.stores = stores
        self.calls = []
        self.unavailable = set()

    def exec_argv(self, request, argv, **kwargs):
        from types import SimpleNamespace

        name = getattr(request, "name", request)
        self.calls.append((name, argv))
        assert argv[:3] == ("docker", "image", "inspect") or argv[:2] == (
            "skopeo",
            "inspect",
        )
        reference = argv[-1]
        digest = self.stores.get(name, {}).get(reference)
        return SimpleNamespace(
            return_code=0 if digest and name not in self.unavailable else 1,
            stdout=digest or "",
            stderr="image or VM unavailable",
        )


def test_local_image_verifier_uses_exact_frozen_daemon_mapping():
    arm_ref = "docker-daemon:registry/nanofaas/amd64-named-target:v1-arm64"
    amd_ref = "docker-daemon:registry/nanofaas/arm64-named-target:v1-amd64"
    arm_id, amd_id, wrong = ("sha256:" + char * 64 for char in "abc")
    provider = DaemonProvider(
        {
            "stack": {amd_ref.removeprefix("docker-daemon:"): amd_id},
            "arm": {arm_ref.removeprefix("docker-daemon:"): arm_id},
        }
    )
    mapping = {arm_ref: "arm", amd_ref: "stack"}
    verify = release_evidence_verifiers(
        provider, "stack", local_image_requests=mapping
    )["local-image-digest"]
    assert verify(Evidence("local-image-digest", arm_ref, arm_id))
    assert provider.calls == [
        (
            "arm",
            (
                "docker",
                "image",
                "inspect",
                "--format={{.Id}}",
                arm_ref.removeprefix("docker-daemon:"),
            ),
        )
    ]
    provider.calls.clear()
    provider.stores["stack"][arm_ref.removeprefix("docker-daemon:")] = arm_id
    provider.stores["arm"][arm_ref.removeprefix("docker-daemon:")] = wrong
    assert not verify(Evidence("local-image-digest", arm_ref, arm_id))
    assert [name for name, _argv in provider.calls] == ["arm"]
    provider.calls.clear()
    assert verify(Evidence("local-image-digest", amd_ref, amd_id))
    assert [name for name, _argv in provider.calls] == ["stack"]


def test_authoritative_local_mapping_fails_closed_without_fallback():
    digest = "sha256:" + "a" * 64
    ref = "docker-daemon:registry/image:arm64"
    provider = DaemonProvider({"stack": {"registry/image:arm64": digest}})
    mapping = {ref: "arm"}
    verify = release_evidence_verifiers(
        provider, "stack", local_image_requests=mapping
    )["local-image-digest"]
    for unknown in (
        "docker-daemon:registry/image:arm64-other",
        "docker://registry/image:arm64",
    ):
        assert not verify(Evidence("local-image-digest", unknown, digest))
    assert not provider.calls
    provider.unavailable.add("arm")
    assert not verify(Evidence("local-image-digest", ref, digest))
    assert [name for name, _argv in provider.calls] == ["arm"]
    provider.calls.clear()
    empty = release_evidence_verifiers(provider, "stack", local_image_requests={})[
        "local-image-digest"
    ]
    assert not empty(Evidence("local-image-digest", ref, digest))
    assert not provider.calls


def test_daemon_mapping_cannot_redirect_registry_or_authenticated_ghcr():
    digest = "sha256:" + "a" * 64
    ref = "docker://registry/image:arm64"
    ghcr = "docker://ghcr.io/nanofaas/image:arm64"
    provider = DaemonProvider({"stack": {ref: digest, ghcr: digest}})
    verifiers = release_evidence_verifiers(
        provider,
        "stack",
        local_image_requests={ref: "arm", ghcr: "arm"},
        ghcr_authfile="/auth.json",
    )
    assert verifiers["local-registry-digest"](
        Evidence("local-registry-digest", ref, digest)
    )
    assert verifiers["ghcr-digest"](Evidence("ghcr-digest", ghcr, digest))
    assert [name for name, _argv in provider.calls] == ["stack", "stack"]
    assert "--authfile=/auth.json" in provider.calls[1][1]


def test_standalone_daemon_verifier_preserves_stack_default():
    digest = "sha256:" + "a" * 64
    provider = DaemonProvider({"stack": {"image:arm64": digest}})
    verify = release_evidence_verifiers(provider, "stack")["local-image-digest"]
    assert verify(Evidence("local-image-digest", "docker-daemon:image:arm64", digest))
    assert [name for name, _argv in provider.calls] == ["stack"]
