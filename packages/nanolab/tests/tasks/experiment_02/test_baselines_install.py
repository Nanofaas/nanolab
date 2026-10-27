"""Baselines are installed once per identity, verified, and never silently stale."""

import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from nanolab.tasks.experiment_02 import baselines
from nanolab.tasks.experiment_02.baselines import FNSERVER, install_baselines
from nanolab.tasks.experiment_02.workload import ARTIFACTS, ensure_workloads

from .fakes import RecordingProvider, Result, vm

SOURCE = PurePosixPath("/home/azureuser/e02/workloads/qr-code")
ARTIFACT_SHA = {key: f"{index}" * 64 for index, key in enumerate(ARTIFACTS)}


def _scripts(provider: RecordingProvider) -> str:
    return "\n".join(
        argv[-1] for argv in provider.commands() if argv[:2] == ("sudo", "sh")
    )


def _installer() -> RecordingProvider:
    def handler(argv, _env):
        if argv[:2] == ("sudo", "touch"):
            provider.files[argv[2]] = b""

    provider = RecordingProvider(handler=handler)
    return provider


def test_install_verifies_spin_checksum_and_image_digests():
    provider = _installer()
    install_baselines(
        vm(provider), ("spin", "funless", "fn"), source=SOURCE, artifacts=ARTIFACT_SHA
    )
    scripts = _scripts(provider)
    assert "checksums-v4.2.5.txt" in scripts and "sha256sum -c" in scripts
    assert "funlessdev/core@sha256:c75155af" in scripts
    assert FNSERVER in scripts
    assert "tinyfaas" not in scripts  # only the selected platforms


def test_install_is_idempotent_per_identity():
    provider = _installer()
    first = install_baselines(
        vm(provider), ("fn",), source=SOURCE, artifacts=ARTIFACT_SHA
    )
    count = len(provider.commands())
    again = install_baselines(
        vm(provider), ("fn",), source=SOURCE, artifacts=ARTIFACT_SHA
    )
    assert again["fn"]["root"] == first["fn"]["root"]
    assert all(argv[:2] != ("sudo", "sh") for argv in provider.commands()[count:])
    # Other wrappers are another installation, beside the first, never over it.
    changed = install_baselines(
        vm(provider),
        ("fn",),
        source=SOURCE,
        artifacts={**ARTIFACT_SHA, "qr-fn": "f" * 64},
    )
    assert changed["fn"]["root"] != first["fn"]["root"]
    assert changed["fn"]["image"] != first["fn"]["image"]
    assert str(first["fn"]["image"]).endswith(str(first["fn"]["identity"]))


def test_install_records_what_it_observed():
    def handler(argv, _env):
        if argv[:2] == ("sh", "-c") and "RepoDigests" in argv[-1]:
            return Result(0, "fnproject/fnserver@sha256:16dedb58\n")
        if argv[:2] == ("sudo", "touch"):
            provider.files[argv[2]] = b""
        return None

    provider = RecordingProvider(handler=handler)
    installed = install_baselines(
        vm(provider), ("fn",), source=SOURCE, artifacts=ARTIFACT_SHA
    )
    assert "fnserver@sha256:16dedb58" in str(installed["fn"]["observed"])


def _run_script(tmp_path: Path, monkeypatch, key: str, fail: str) -> tuple[int, str]:
    """Run a generated install script for real, with fake tools; `fail` exits 1."""
    calls = tmp_path / "calls"
    tools = tmp_path / "bin"
    tools.mkdir()
    for tool in ("curl", "grep", "sha256sum", "tar", "cp", "docker"):
        code = 1 if tool == fail else 0
        (tools / tool).write_text(
            f'#!/bin/sh\necho {tool} "$@" >> {calls}\nexit {code}\n'
        )
        (tools / tool).chmod(0o755)
    root = tmp_path / "opt" / key / "x"
    root.mkdir(parents=True)
    script = baselines._install_script(key, SOURCE, PurePosixPath(root), "x")
    done = subprocess.run(
        ("sh", "-c", f"set -e\n{script}"),
        env={**os.environ, "PATH": f"{tools}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, calls.read_text() if calls.exists() else ""


@pytest.mark.parametrize(
    ("key", "fail", "never"),
    [
        ("spin", "sha256sum", "tar "),  # a checksum mismatch never extracts
        ("spin", "curl", "tar "),
        ("fn", "docker", "docker push"),  # a failed pull or build never pushes
    ],
)
def test_a_failed_step_stops_the_install(tmp_path, monkeypatch, key, fail, never):
    code, calls = _run_script(tmp_path, monkeypatch, key, fail)
    assert code != 0
    assert never not in calls


def test_a_failed_install_writes_no_marker():
    def handler(argv, _env):
        if argv[:2] == ("sudo", "sh"):
            return Result(1, "", "checksum mismatch")
        return None

    provider = RecordingProvider(handler=handler)
    with pytest.raises(RuntimeError):
        install_baselines(
            vm(provider), ("spin",), source=SOURCE, artifacts=ARTIFACT_SHA
        )
    assert not any(argv[:2] == ("sudo", "touch") for argv in provider.commands())


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
