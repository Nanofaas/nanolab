"""A faasd preparation is bound to its workload and verified install."""

import json
import os
import subprocess
from pathlib import PurePosixPath

import pytest

from nanolab.tasks.experiment_02.faasd_preparation import (
    _verification,
    ensure_faasd,
    faasd_identity,
    require_record,
)

from .fakes import RecordingProvider, Result, vm


def test_faasd_identity_changes_with_the_shared_artifact_or_tree():
    first = faasd_identity(artifact_sha256="a" * 64, workload_tree="b" * 40)
    assert faasd_identity(artifact_sha256="c" * 64, workload_tree="b" * 40) != first
    assert faasd_identity(artifact_sha256="a" * 64, workload_tree="c" * 40) != first


def test_resume_refuses_changed_faasd_inputs_before_remote_work(tmp_path):
    record = tmp_path / "faasd.json"
    record.write_text(json.dumps({"identity": "old", "image": "local/qr:old"}))
    with pytest.raises(ValueError, match="faasd installation changed"):
        require_record(record, "new")
    saved = require_record(record, "old")
    assert saved is not None
    assert saved["image"] == "local/qr:old"
    assert require_record(tmp_path / "missing.json", "old") is None


def test_recorded_faasd_installation_only_verifies_on_resume(tmp_path):
    identity = faasd_identity(artifact_sha256="a" * 64, workload_tree="b" * 40)
    record = tmp_path / "faasd.json"
    saved = {
        "identity": identity,
        "image": f"127.0.0.1:5000/qr-faasd:{identity}",
        "verification": "verified",
        "root": "/var/lib/faasd",
    }
    record.write_text(json.dumps(saved))
    provider = RecordingProvider(handler=lambda _argv, _env: Result(0, "verified"))
    assert (
        ensure_faasd(
            vm(provider),
            vm(provider),
            source=PurePosixPath("/work/qr-code"),
            artifact_sha256="a" * 64,
            workload_tree="b" * 40,
            remote_root=PurePosixPath("/work"),
            record=record,
        )
        == saved
    )
    commands = provider.commands()
    assert len(commands) == 1
    assert not any("install" in " ".join(argv) for argv in commands)


def test_new_faasd_preparation_builds_on_stack_and_verifies_archive_before_install(
    tmp_path,
):
    identity = faasd_identity(artifact_sha256="a" * 64, workload_tree="b" * 40)
    remote = PurePosixPath("/work")

    def build(argv, _env):
        if argv[:2] == ("sh", "-c") and "docker save" in argv[-1]:
            stack.files[str(remote / f"faasd-{identity}.tar")] = b"image archive"
        return

    stack = RecordingProvider(handler=build)
    native = RecordingProvider(handler=lambda _argv, _env: Result(0, "verified"))
    record = tmp_path / "faasd.json"
    installed = ensure_faasd(
        vm(stack),
        vm(native),
        source=remote / "qr-code",
        artifact_sha256="a" * 64,
        workload_tree="b" * 40,
        remote_root=remote,
        record=record,
    )
    assert json.loads(record.read_text()) == installed
    commands = native.commands()
    verify = next(
        i for i, argv in enumerate(commands) if "| sha256sum -c -" in argv[-1]
    )
    install = next(i for i, argv in enumerate(commands) if "faasd-install" in argv)
    assert verify < install
    assert not any("docker build" in " ".join(argv) for argv in commands)
    assert any("skopeo" in argv and "copy" in argv for argv in commands)


def test_new_faasd_preparation_prefetches_function_layers_before_cells(tmp_path):
    identity = faasd_identity(artifact_sha256="a" * 64, workload_tree="b" * 40)
    image = f"127.0.0.1:5000/qr-faasd:{identity}"
    stack = RecordingProvider()
    stack.files[f"/work/faasd-{identity}.tar"] = b"archive"
    native = RecordingProvider(handler=lambda _argv, _env: Result(0, "verified"))
    ensure_faasd(
        vm(stack),
        vm(native),
        source=PurePosixPath("/work/qr-code"),
        artifact_sha256="a" * 64,
        workload_tree="b" * 40,
        remote_root=PurePosixPath("/work"),
        record=tmp_path / "faasd.json",
    )
    assert (
        "sudo",
        "ctr",
        "-n",
        "openfaas-fn",
        "images",
        "pull",
        "--plain-http",
        image,
    ) in native.commands()


@pytest.mark.parametrize(
    "component",
    [
        "/usr/local/bin/containerd",
        "runc",
        "/opt/cni/bin/bridge",
        "/etc/systemd/system/containerd.service",
    ],
)
def test_resume_refuses_changed_runtime_components(tmp_path, component):
    identity = faasd_identity(artifact_sha256="a" * 64, workload_tree="b" * 40)
    record = tmp_path / "faasd.json"
    record.write_text(
        json.dumps(
            {
                "identity": identity,
                "image": f"127.0.0.1:5000/qr-faasd:{identity}",
                "verification": "verified",
                "root": "/var/lib/faasd",
            }
        )
    )
    provider = RecordingProvider(
        handler=lambda argv, _env: Result(
            0,
            "changed" if component in " ".join(argv) else "verified",
        )
    )
    with pytest.raises(ValueError, match="drifted"):
        ensure_faasd(
            vm(provider),
            vm(provider),
            source=PurePosixPath("/work/qr-code"),
            artifact_sha256="a" * 64,
            workload_tree="b" * 40,
            remote_root=PurePosixPath("/work"),
            record=record,
        )


def test_verification_does_not_hide_a_failed_image_inventory(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, script in {
        "sha256sum": "echo hash",
        "systemctl": "echo unit",
        "containerd": "echo version",
        "runc": "echo version",
        "ctr": "exit 1",
    }.items():
        path = bindir / name
        path.write_text(f"#!/bin/sh\n{script}\n")
        path.chmod(0o755)

    def execute(argv, _env):
        result = subprocess.run(
            ("sh", "-c", argv[-1]),
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{bindir}:{os.defpath}"},
        )
        return Result(result.returncode, result.stdout, result.stderr)

    with pytest.raises(RuntimeError, match="Comparison VM command failed"):
        _verification(
            vm(RecordingProvider(handler=execute)), "127.0.0.1:5000/qr-faasd:id"
        )
