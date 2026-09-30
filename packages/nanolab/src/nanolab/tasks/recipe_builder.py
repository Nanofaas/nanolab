"""Owned Buildx and scoped binfmt resources for host recipe publication."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks.command import CommandTask
from sonata_tasks.docker import DockerTask
from sonata_tasks.execution.ports import CommandTaskExecutor

BINFMT_INSTALLER_IMAGE = (
    "tonistiigi/binfmt@sha256:"
    "400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
)
FOREIGN_PROBE_IMAGE = (
    "busybox@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)

_REGISTRATION_SCRIPT = """set -eu
root=/proc/sys/fs/binfmt_misc
mkdir -p "$root"
if [ ! -f "$root/status" ]; then mount -t binfmt_misc binfmt_misc "$root"; fi
entry="$root/$1"
case "$2" in
inspect-registration) if [ -f "$entry" ]; then cat "$entry"; fi ;;
remove-registration)
    if [ ! -f "$entry" ]; then exit 42; fi
    current=$(cat "$entry")
    if [ "$current" != "$3" ]; then exit 42; fi
    printf '%s\\n' -1 > "$entry"
    ;;
*) exit 43 ;;
esac
"""


@dataclass(frozen=True, slots=True)
class RecipeBuilder:
    """Explicit builder and the native Docker platform selected for runtime."""

    name: str
    platform: str


def recipe_builder_resource(
    *,
    executor: CommandTaskExecutor,
    run_dir: Path,
    tag: str,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[RecipeBuilder]:
    """Acquire emulation and a private builder, compensating partial failure."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,80}", tag):
        raise ValueError("Invalid recipe builder tag")
    name = f"nanolab-{tag}"
    lock_fd: int | None = None
    owned_builder = False
    owned_registration: str | None = None
    registration_name = ""
    evidence: dict[str, object] = {}

    def command(inputs: TaskInputs, argv: tuple[str, ...], title: str) -> str:
        result = (
            CommandTask(argv=argv, executor=executor, role="host", title=title)
            .run(inputs)
            .value
        )
        if result is None:
            raise RuntimeError(f"{title} returned no command result")
        return result.stdout

    def registration(
        inputs: TaskInputs, operation: str, expected: str | None = None
    ) -> str:
        result = (
            DockerTask(
                "run",
                "--rm",
                "--privileged",
                FOREIGN_PROBE_IMAGE,
                "sh",
                "-c",
                _REGISTRATION_SCRIPT,
                "binfmt-helper",
                registration_name,
                operation,
                *((expected,) if expected is not None else ()),
                executor=executor,
                role="host",
                title=f"Recipe binfmt {operation}",
            )
            .run(inputs)
            .value
        )
        if result is None:
            raise RuntimeError("Registration helper returned no result")
        return result.stdout.strip()

    def cleanup(inputs: TaskInputs) -> None:
        nonlocal lock_fd, owned_builder, owned_registration
        errors: list[str] = []
        try:
            if owned_builder:
                try:
                    command(
                        inputs,
                        ("docker", "buildx", "rm", name),
                        "Remove owned recipe builder",
                    )
                    owned_builder = False
                except Exception as error:
                    errors.append(str(error))
            if owned_registration is not None and not owned_builder:
                try:
                    current = registration(inputs, "inspect-registration")
                    if current != owned_registration:
                        raise RuntimeError("Owned binfmt registration cleanup conflict")
                    registration(inputs, "remove-registration", owned_registration)
                    owned_registration = None
                except Exception as error:
                    errors.append(f"registration cleanup conflict: {error}")
            if owned_registration is not None and owned_builder:
                errors.append(
                    "Retained registration because owned builder cleanup failed"
                )
            if run_dir.exists():
                (run_dir / "builder-cleanup.json").write_text(
                    json.dumps(
                        {
                            "builder": name,
                            "errors": errors,
                            "registrationRetained": owned_registration is not None,
                        },
                        indent=2,
                    )
                    + "\n"
                )
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
                lock_fd = None
        if errors:
            raise RuntimeError("Recipe builder cleanup failed: " + "; ".join(errors))

    def acquire(inputs: TaskInputs) -> RecipeBuilder:
        nonlocal lock_fd, owned_builder, owned_registration, registration_name
        info = json.loads(
            command(
                inputs,
                (
                    "docker",
                    "info",
                    "--format",
                    '{"id":{{json .ID}},"os":{{json .OSType}},'
                    '"architecture":{{json .Architecture}}}',
                ),
                "Inspect Docker daemon platform",
            )
        )
        architecture = {
            "amd64": "amd64",
            "x86_64": "amd64",
            "arm64": "arm64",
            "aarch64": "arm64",
        }.get(info.get("architecture"))
        if info.get("os") != "linux" or architecture is None or not info.get("id"):
            raise RuntimeError(
                "Multiarch recipe needs a Linux AMD64 or ARM64 Docker daemon"
            )
        foreign = "amd64" if architecture == "arm64" else "arm64"
        registration_name = "qemu-x86_64" if foreign == "amd64" else "qemu-aarch64"
        names = command(
            inputs,
            ("docker", "buildx", "ls", "--format", "{{.Name}}"),
            "Inspect existing builders",
        )
        if name in {line.strip().rstrip("*") for line in names.splitlines()}:
            raise RuntimeError(f"Recipe builder {name} already exists")
        lock_name = (
            "nanolab-binfmt-"
            + hashlib.sha256(info["id"].encode()).hexdigest()[:16]
            + ".lock"
        )
        fd = os.open(
            Path(tempfile.gettempdir()) / lock_name,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.getuid():
            os.close(fd)
            raise RuntimeError("Unsafe binfmt lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(fd)
            raise RuntimeError("NanoLab binfmt lock is busy") from error
        lock_fd = fd
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            before = registration(inputs, "inspect-registration")
            evidence.update(
                daemon=info,
                existingBuilders=names,
                registrationBefore=before,
                installer=BINFMT_INSTALLER_IMAGE,
                probe=FOREIGN_PROBE_IMAGE,
                builder=name,
            )
            if before and (
                not before.startswith("enabled\n")
                or "F" not in before.split("flags:")[-1].strip()
            ):
                raise RuntimeError(
                    "Existing binfmt registration is disabled or incompatible"
                )
            if not before:
                try:
                    command(
                        inputs,
                        (
                            "docker",
                            "run",
                            "--rm",
                            "--privileged",
                            BINFMT_INSTALLER_IMAGE,
                            "--install",
                            foreign,
                        ),
                        "Install scoped recipe emulation",
                    )
                finally:
                    installed = registration(inputs, "inspect-registration")
                    if installed:
                        owned_registration = installed
                        evidence["ownedRegistration"] = installed
                        (run_dir / "builder.json").write_text(
                            json.dumps(evidence, indent=2) + "\n"
                        )
                if owned_registration is None:
                    raise RuntimeError("Emulation installer created no registration")
            command(
                inputs,
                (
                    "docker",
                    "run",
                    "--rm",
                    "--platform",
                    f"linux/{foreign}",
                    FOREIGN_PROBE_IMAGE,
                    "/bin/true",
                ),
                "Probe foreign recipe platform",
            )
            config = run_dir / "buildkitd.toml"
            config.write_text('[registry."127.0.0.1:5000"]\n  http = true\n')
            command(
                inputs,
                (
                    "docker",
                    "buildx",
                    "create",
                    "--name",
                    name,
                    "--driver",
                    "docker-container",
                    "--driver-opt",
                    "network=host",
                    "--buildkitd-config",
                    str(config),
                ),
                "Create owned recipe builder",
            )
            owned_builder = True
            bootstrap = command(
                inputs,
                (
                    "docker",
                    "buildx",
                    "inspect",
                    "--bootstrap",
                    name,
                ),
                "Bootstrap recipe builder",
            )
            evidence["bootstrap"] = bootstrap
            platforms = {
                platform.strip().rstrip("*")
                for line in re.findall(
                    r"^\s*Platforms:\s*(.*)$", bootstrap, re.MULTILINE
                )
                for platform in line.split(",")
            }
            if not {"linux/amd64", "linux/arm64"}.issubset(platforms):
                raise RuntimeError(
                    "Recipe builder does not advertise both required platforms"
                )
            (run_dir / "builder.json").write_text(json.dumps(evidence, indent=2) + "\n")
            return RecipeBuilder(name, f"linux/{architecture}")
        except BaseException as error:
            evidence["failure"] = str(error)
            (run_dir / "builder.json").write_text(json.dumps(evidence, indent=2) + "\n")
            try:
                cleanup(inputs)
            except Exception as cleanup_error:
                error.add_note(str(cleanup_error))
            raise

    return Resource(
        title="Prepare multiarch recipe builder",
        acquire=acquire,
        release=lambda inputs, _value: cleanup(inputs),
        requires=requires,
    )
