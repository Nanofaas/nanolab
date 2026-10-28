"""The paper's qr-code wrappers: one committed source, built once, checked by PNG."""

import base64
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from nanolab.tasks.experiment_02.workload import (
    ARTIFACTS,
    BUILDER,
    MEASURED_PNG_SHA256,
    build_workloads,
    capture_workloads,
)

from .fakes import RecordingProvider, Result, vm

REMOTE = PurePosixPath("/home/azureuser/e02/workloads")
PNG = b"\x89PNG\r\n\x1a\nfake"


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "paper"
    (root / "functions" / "qr-code").mkdir(parents=True)
    (root / "functions" / "qr-code" / "Cargo.toml").write_text("[workspace]\n")

    def git(*args: str) -> None:
        subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "tests@example.com")
    git("config", "user.name", "Tests")
    git("add", ".")
    git("commit", "-qm", "workload")
    return root / "functions"


def test_capture_records_commit_and_tree(tmp_path):
    functions = _repository(tmp_path)
    first = capture_workloads(functions)
    assert first == capture_workloads(functions)
    assert set(first) == {"commit", "tree"}
    (functions / "qr-code" / "README.md").write_text("x\n")
    subprocess.run(("git", "add", "."), cwd=functions, check=True)
    subprocess.run(("git", "commit", "-qm", "more"), cwd=functions, check=True)
    assert capture_workloads(functions)["tree"] != first["tree"]


def test_dirty_or_moved_workload_source_is_refused(tmp_path):
    functions = _repository(tmp_path)
    (functions / "qr-code" / "Cargo.toml").write_text("[workspace]\nmembers=[]\n")
    with pytest.raises(ValueError, match="dirty"):
        capture_workloads(functions)
    with pytest.raises(ValueError, match="qr-code"):
        capture_workloads(tmp_path)


def _stack(png: bytes) -> RecordingProvider:
    def handler(argv, _env):
        if argv[:2] == ("sh", "-c") and "qr-stdio" in argv[-1]:
            return Result(0, json.dumps(base64.b64encode(png).decode()))
        if BUILDER in argv:
            for path in ARTIFACTS.values():
                provider.files[str(REMOTE / "qr-code" / path)] = path.encode()
        return None

    provider = RecordingProvider(handler=handler)
    return provider


def test_build_runs_the_pinned_builder_and_checks_the_png(tmp_path):
    functions = _repository(tmp_path)
    expected = hashlib.sha256(PNG).hexdigest()
    provider = _stack(PNG)
    artifacts = build_workloads(
        vm(provider),
        workloads=functions,
        remote_root=REMOTE,
        payload=b'{"text":"A","size":128}',
        png_sha256=expected,
    )
    assert artifacts == {
        key: hashlib.sha256(path.encode()).hexdigest()
        for key, path in ARTIFACTS.items()
    }
    [build] = [argv for argv in provider.commands() if BUILDER in argv]
    script = build[-1]
    assert "--locked" in script
    for package in ("qr-stdio", "qr-fn", "qr-funless", "qr-spin"):
        assert f"-p {package}" in script
    with pytest.raises(ValueError, match="png"):
        build_workloads(
            vm(_stack(b"\x89PNG\r\n\x1a\nother")),
            workloads=functions,
            remote_root=REMOTE,
            payload=b'{"text":"A","size":128}',
            png_sha256=expected,
        )


def test_without_a_frozen_hash_the_measured_png_is_required(tmp_path):
    functions = _repository(tmp_path)
    with pytest.raises(ValueError, match=MEASURED_PNG_SHA256[:8]):
        build_workloads(
            vm(_stack(PNG)),
            workloads=functions,
            remote_root=REMOTE,
            payload=b"{}",
            png_sha256=None,
        )


def test_build_uses_the_identity_commit_after_head_advances(tmp_path):
    functions = _repository(tmp_path)
    identity = capture_workloads(functions)
    (functions / "qr-code" / "Cargo.toml").write_text("changed\n")
    subprocess.run(("git", "add", "."), cwd=functions, check=True)
    subprocess.run(("git", "commit", "-qm", "advance"), cwd=functions, check=True)
    provider = _stack(PNG)
    build_workloads(
        vm(provider),
        workloads=functions,
        remote_root=REMOTE,
        payload=b"{}",
        png_sha256=hashlib.sha256(PNG).hexdigest(),
        workload_commit=identity["commit"],
    )
    import io
    import tarfile

    archive = provider.files[str(REMOTE / "qr-code.tar")]
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        member = source.extractfile("qr-code/Cargo.toml")
        assert member is not None
        assert member.read() == b"[workspace]\n"
