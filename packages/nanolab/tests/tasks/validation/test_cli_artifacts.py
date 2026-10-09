"""Reject changed or incompatible shipped CLI artifacts; fake ELF is parser-only."""

from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path

import pytest
from sonata_engine import Resource, TaskInputs
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from nanolab.workspace.recipe import RecipeRun

JVM = Path("clients/cli/build/install/nanofaas-cli/bin/nanofaas-cli")
NATIVE = Path("clients/cli/build/native/nativeCompile/nanofaas-cli")
LIB = JVM.parent.parent / "lib/cli.jar"
ARCH = "arm64" if platform.machine() == "aarch64" else "amd64"


def _source(root: Path, mode: str) -> Path:
    root.mkdir(exist_ok=True)
    (root / "build.gradle").write_text("version = '0.22.0'\n")
    binary = root / (JVM if mode == "jvm" else NATIVE)
    binary.parent.mkdir(parents=True, exist_ok=True)
    if mode == "jvm":
        binary.write_text("#!/bin/sh\nexit 0\n")
        (root / LIB).parent.mkdir(parents=True)
        (root / LIB).write_bytes(b"jar fixture")
    else:
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        header[18:20] = (183 if ARCH == "arm64" else 62).to_bytes(2, "little")
        binary.write_bytes(header)
    binary.chmod(0o755)
    return binary


def test_cli_modes_selects_exact_artifacts():
    from nanolab.tasks.cli_artifacts import cli_modes

    assert cli_modes("jvm") == ("jvm",)
    assert cli_modes("native") == ("native",)
    assert cli_modes("parity") == ("jvm", "native")
    with pytest.raises(ValueError, match="Unsupported CLI runtime"):
        cli_modes("unknown")


@pytest.mark.parametrize("mode", ["jvm", "native"])
@pytest.mark.parametrize(
    "mutation",
    ["binary", "library", "missing", "symlink", "chmod", "wrong-machine", "script"],
)
def test_artifact_identity_checks_kind_arch_and_libraries(tmp_path, mode, mutation):
    from nanolab.tasks.cli_artifacts import describe_cli_artifact, verify_cli_artifact

    if mode == "jvm" and mutation in ("wrong-machine", "script"):
        pytest.skip("ELF requirement belongs to native")
    if mode == "native" and mutation == "library":
        pytest.skip("Installed libraries belong to JVM")
    binary = _source(tmp_path, mode)
    artifact = describe_cli_artifact(tmp_path, mode, architecture=ARCH)
    assert artifact.version == "0.22.0"
    verify_cli_artifact(tmp_path, artifact)
    if mutation == "library":
        (tmp_path / LIB).write_bytes(b"changed library")
    elif mutation == "missing":
        binary.unlink()
    elif mutation == "chmod":
        binary.chmod(0o644)
    elif mutation == "symlink":
        binary.unlink()
        binary.symlink_to("/bin/sh")
    elif mutation == "wrong-machine":
        raw = bytearray(binary.read_bytes())
        raw[18:20] = (3).to_bytes(2, "little")
        binary.write_bytes(raw)
    else:
        binary.write_bytes(
            b"#!/bin/sh\nexit 0\n" if mutation == "script" else b"changed"
        )
    with pytest.raises((ValueError, OSError), match=r"CLI|artifact|ELF|executable"):
        verify_cli_artifact(tmp_path, artifact)


class BuildExecutor:
    def __init__(self, source: Path, mode: str, fail: str | None = None):
        self.source, self.mode, self.fail = source, mode, fail
        self.specs: list[CommandTaskSpec] = []

    def binding_key(self, role: str) -> str:
        return role

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.specs.append(task)
        assert task.role == "host"
        assert task.options.cwd == self.source
        if task.argv[0] == "./gradlew":
            _source(self.source, self.mode)
            if self.fail == "inputs":
                (self.source / "build.gradle").write_text("version = '0.22.1'\n")
            stdout = "BUILD SUCCESSFUL"
        else:
            stdout = "Usage: nanofaas" if "--help" in task.argv else "nanofaas 0.22.0"
        if self.fail == "version" and "--version" in task.argv:
            stdout = "nanofaas 0.21.0"
        if self.fail == "marker" and "--help" in task.argv:
            stdout += " MissingReflectionRegistrationError"
        if self.fail == "oversize" and "--help" in task.argv:
            stdout = "x" * (1024 * 1024 + 1)
        code = 1 if self.fail == "build" and task.argv[0] == "./gradlew" else 0
        return TaskResult(
            task_id=task.task_id,
            status="failed" if code else "passed",
            return_code=code,
            stdout=stdout,
        )


@pytest.mark.parametrize("mode", ["jvm", "native"])
@pytest.mark.parametrize(
    "fail", [None, "build", "version", "marker", "oversize", "inputs"]
)
def test_artifact_build_depends_only_on_frozen_source(tmp_path, mode, fail):
    from nanolab.tasks.cli_artifacts import cli_artifact_resource

    source = tmp_path / "source"
    source.mkdir()
    (source / "build.gradle").write_text("version = '0.22.0'\n")
    subprocess.run(("git", "init", "-q", str(source)), check=True)
    subprocess.run(("git", "add", "build.gradle"), cwd=source, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ),
        cwd=source,
        check=True,
    )
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("name: cli\n")
    value = RecipeRun(source, recipe, tmp_path / "distribution", "tag-test")
    run = Resource(
        title="Frozen source",
        acquire=lambda _inputs: value,
        release=lambda *_args: None,
    )
    executor = BuildExecutor(source, mode, fail)
    evidence = tmp_path / "evidence"
    resource = cli_artifact_resource(
        run, mode=mode, executor=executor, evidence_dir=evidence
    )
    assert resource.requires == (run,)
    inputs = TaskInputs._for_resources({run: value}, {run})
    if fail:
        with pytest.raises((ValueError, RuntimeError)):
            resource.acquire(inputs)
        assert not (evidence / f"{mode}.json").exists()
    else:
        artifact = resource.acquire(inputs)
        assert artifact.binary == source / (JVM if mode == "jvm" else NATIVE)
        receipt = json.loads((evidence / f"{mode}.json").read_text())
        assert receipt["mode"] == mode
        assert receipt["version"] == "0.22.0"
        assert receipt["source"]["revision"]
        assert receipt["help"]["stdout"] == "Usage: nanofaas"
        with pytest.raises(FileExistsError):
            resource.acquire(inputs)
    assert executor.specs[0].argv == (
        "./gradlew",
        f":nanofaas-cli:{'installDist' if mode == 'jvm' else 'nativeCompile'}",
        "--no-daemon",
    )
    assert not any(
        "kubectl" in spec.argv or "minikube" in spec.argv for spec in executor.specs
    )
