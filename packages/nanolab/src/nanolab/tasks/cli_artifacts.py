"""Build the shipped CLI in frozen source and bind its complete executable identity."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.gradle import GradleTask

from nanolab.assets.diagnostics.native_k8s_runtime import NATIVE_ERRORS
from nanolab.comparison.prepare import captured_source_state
from nanolab.release.versioning import read_project_version
from nanolab.workspace.recipe import RecipeRun

CliMode = Literal["jvm", "native"]
CLI_STREAM_LIMIT = 1024 * 1024
CLI_ERROR_MARKERS = (
    *NATIVE_ERRORS,
    "No serializer found",
    "Traceback (most recent call last)",
    "panicked at",
)
_PATHS = {
    "jvm": Path("clients/cli/build/install/nanofaas-cli/bin/nanofaas-cli"),
    "native": Path("clients/cli/build/native/nativeCompile/nanofaas-cli"),
}


def cli_modes(runtime: str) -> tuple[CliMode, ...]:
    """Select exactly the artifacts the operator requested, with no fallback."""
    match runtime:
        case "jvm":
            return ("jvm",)
        case "native":
            return ("native",)
        case "parity":
            return ("jvm", "native")
        case _:
            raise ValueError(f"Unsupported CLI runtime: {runtime}")


@dataclass(frozen=True, slots=True)
class CliArtifact:
    """Complete executable identity, including installed JVM dependencies."""

    mode: CliMode
    binary: Path
    architecture: str
    version: str
    files: tuple[tuple[str, str], ...]


class CliArtifactCheckTask(Task[None]):
    """Expose artifact-only selection without acquiring the API platform."""

    def __init__(self, source: Resource[RecipeRun], artifact: Resource[CliArtifact]):
        """Bind only the frozen source and selected launcher."""
        self.source, self.artifact = source, artifact
        self.title = artifact.title

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Require the acquired artifact identity to remain unchanged."""
        verify_cli_artifact(
            inputs.resource(self.source).source_dir, inputs.resource(self.artifact)
        )
        return TaskOutcome(value=None)


def _host_architecture() -> str:
    machine = platform.machine()
    return {"aarch64": "arm64", "x86_64": "amd64"}.get(machine, machine)


def _artifact_file(source: Path, path: Path, *, executable: bool = False) -> Path:
    if (
        not path.resolve().is_relative_to(source.resolve())
        or path.is_symlink()
        or not path.is_file()
    ):
        raise ValueError(
            f"CLI artifact must be a regular file inside frozen source: {path}"
        )
    if executable and not os.access(path, os.X_OK):
        raise ValueError(f"CLI artifact is not executable: {path}")
    return path


def describe_cli_artifact(
    source: Path, mode: CliMode, *, architecture: str
) -> CliArtifact:
    """Reject missing, escaped or incompatible artifacts before any CLI execution."""
    cli_modes(mode)
    if architecture not in ("amd64", "arm64"):
        raise ValueError(f"Unsupported CLI artifact architecture: {architecture}")
    binary = _artifact_file(source, source / _PATHS[mode], executable=True)
    files = [binary]
    if mode == "native":
        with binary.open("rb") as stream:
            header = stream.read(64)
        machine = {"amd64": 62, "arm64": 183}[architecture]
        if (
            len(header) != 64
            or header[:6] != b"\x7fELF\x02\x01"
            or int.from_bytes(header[18:20], "little") != machine
        ):
            raise ValueError("CLI native artifact is not a matching ELF64 executable")
    else:
        libraries = sorted((binary.parent.parent / "lib").glob("*.jar"))
        if not libraries:
            raise ValueError("CLI JVM artifact lacks installed libraries")
        for library in libraries:
            _artifact_file(source, library)
            if not library.stat().st_size:
                raise ValueError("CLI JVM artifact has an empty installed library")
        files.extend(libraries)
    return CliArtifact(
        mode,
        binary,
        architecture,
        read_project_version(source),
        tuple(
            (
                str(path.relative_to(source)),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in files
        ),
    )


def verify_cli_artifact(source: Path, artifact: CliArtifact) -> None:
    """Reject binary, installed library, source version or execution-host drift."""
    if artifact.architecture != _host_architecture():
        raise ValueError("CLI artifact is incompatible with the execution host")
    current = describe_cli_artifact(
        source, artifact.mode, architecture=artifact.architecture
    )
    if current != artifact:
        raise ValueError("CLI artifact changed after qualification")


def check_cli_diagnostics(
    stdout: str, stderr: str, *, limit: int = CLI_STREAM_LIMIT
) -> None:
    """Bound evidence and reject runtime registration/serialization failures."""
    if max(len(stdout.encode()), len(stderr.encode())) > limit:
        raise RuntimeError("CLI output exceeds its byte bound")
    for marker in CLI_ERROR_MARKERS:
        if marker in stdout or marker in stderr:
            raise RuntimeError(f"CLI runtime diagnostic: {marker}")


def cli_artifact_resource(
    run: Resource[RecipeRun],
    *,
    mode: CliMode,
    executor: CommandTaskExecutor,
    evidence_dir: Path,
) -> Resource[CliArtifact]:
    """Build only in frozen source and retain independently checked evidence."""
    cli_modes(mode)

    def acquire(inputs: TaskInputs) -> CliArtifact:
        receipt = evidence_dir / f"{mode}.json"
        if receipt.exists():
            raise FileExistsError(receipt)
        staged = inputs.resource(run)
        before = captured_source_state(staged.source_dir)
        recipe_hash = hashlib.sha256(staged.recipe.read_bytes()).hexdigest()
        GradleTask(
            f":nanofaas-cli:{'installDist' if mode == 'jvm' else 'nativeCompile'}",
            executor=executor,
            role="host",
            options=CommandOptions(cwd=staged.source_dir),
        ).run(inputs)
        artifact = describe_cli_artifact(
            staged.source_dir, mode, architecture=_host_architecture()
        )
        verify_cli_artifact(staged.source_dir, artifact)
        evidence: dict[str, object] = {}
        for case in ("help", "version"):
            result = (
                CommandTask(
                    title=f"CLI {mode} {case}",
                    argv=(
                        "timeout",
                        "--kill-after=5s",
                        "60s",
                        str(artifact.binary),
                        f"--{case}",
                    ),
                    executor=executor,
                    role="host",
                    options=CommandOptions(cwd=staged.source_dir),
                )
                .run(inputs)
                .value
            )
            if result is None:
                raise RuntimeError(f"CLI {case} returned no evidence")
            check_cli_diagnostics(result.stdout, result.stderr)
            if case == "help" and "Usage:" not in result.stdout:
                raise RuntimeError("CLI help lacks its usage contract")
            if case == "version" and result.stdout.strip() not in (
                artifact.version,
                f"nanofaas {artifact.version}",
            ):
                raise RuntimeError("CLI version differs from frozen source")
            evidence["versionCommand" if case == "version" else case] = {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returnCode": result.return_code,
            }
        if (
            before != captured_source_state(staged.source_dir)
            or recipe_hash != hashlib.sha256(staged.recipe.read_bytes()).hexdigest()
        ):
            raise RuntimeError("CLI build changed frozen tracked inputs")
        verify_cli_artifact(staged.source_dir, artifact)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        with receipt.open("x") as stream:
            json.dump(
                {
                    **asdict(artifact),
                    "binary": str(artifact.binary),
                    "source": before,
                    "recipeSha256": recipe_hash,
                    "tag": staged.tag,
                    **evidence,
                },
                stream,
                indent=2,
            )
            stream.write("\n")
        return artifact

    return Resource(
        title=f"Build and verify {mode} CLI",
        acquire=acquire,
        release=lambda *_args: None,
        requires=(run,),
    )
