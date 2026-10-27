"""Baselines are installed once per run, verified, and never silently changed."""

import json
from pathlib import Path, PurePosixPath

import pytest

from nanolab.tasks.experiment_02.baselines import FNSERVER, install_baselines
from nanolab.tasks.experiment_02.workload import ARTIFACTS, ensure_workloads

from .fakes import RecordingProvider, Result, vm

SOURCE = PurePosixPath("/home/azureuser/e02/workloads/qr-code")
ARTIFACT_SHA = {key: f"{index}" * 64 for index, key in enumerate(ARTIFACTS)}


def _scripts(provider: RecordingProvider) -> str:
    return "\n".join(
        argv[-1] for argv in provider.commands() if argv[:2] == ("sudo", "sh")
    )


def test_install_verifies_spin_checksum_and_image_digests(tmp_path):
    provider = RecordingProvider(
        handler=lambda argv, _env: Result(1) if argv[0] == "test" else None
    )
    install_baselines(
        vm(provider), ("spin", "funless", "fn"), source=SOURCE, artifacts=ARTIFACT_SHA
    )
    scripts = _scripts(provider)
    assert "checksums-v4.2.5.txt" in scripts and "sha256sum -c" in scripts
    assert "funlessdev/core@sha256:c75155af" in scripts
    assert FNSERVER in scripts
    assert "tinyfaas" not in scripts  # only the selected platforms


def test_install_is_idempotent():
    def handler(argv, _env):
        if argv[:2] == ("sudo", "sh"):
            # The script ends by touching its marker.
            provider.files[argv[-1].rsplit("touch ", 1)[-1].strip()] = b""

    provider = RecordingProvider(handler=handler)
    install_baselines(vm(provider), ("spin",), source=SOURCE, artifacts=ARTIFACT_SHA)
    first = len(provider.commands())
    install_baselines(vm(provider), ("spin",), source=SOURCE, artifacts=ARTIFACT_SHA)
    again = provider.commands()[first:]
    assert all(argv[:2] != ("sudo", "sh") for argv in again)
    # Other artifacts are a different installation.
    changed = {**ARTIFACT_SHA, "qr-spin": "f" * 64}
    install_baselines(vm(provider), ("spin",), source=SOURCE, artifacts=changed)
    assert any(
        argv[:2] == ("sudo", "sh") for argv in provider.commands()[first + len(again) :]
    )


def test_resume_refuses_changed_artifacts(tmp_path: Path):
    record = tmp_path / "workloads.json"
    record.write_text(json.dumps(ARTIFACT_SHA))
    provider = RecordingProvider()
    for key, path in ARTIFACTS.items():
        provider.files[str(SOURCE / path)] = key.encode()  # hashes differ
    with pytest.raises(ValueError, match="changed"):
        ensure_workloads(
            vm(provider),
            workloads=tmp_path,
            remote_root=SOURCE.parent,
            payload=b"{}",
            png_sha256=None,
            record=record,
        )
