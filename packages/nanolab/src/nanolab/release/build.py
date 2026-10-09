"""Release image build, staging, digest, and ARM smoke helpers."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sonata_tasks.archive import (
    SOURCE_ARCHIVE_EXTRACT_SCRIPT as _EXTRACT_ARCHIVE_SCRIPT,
)
from sonata_tasks.archive import stage_source_archive as _stage_source_archive
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.tasks.models import CommandTaskSpec
from sonata_tasks.transfer import RemoteOperationResult

from nanolab.images.plan import ImagePlan
from nanolab.release import arm
from nanolab.release.model import (
    Amd64ReleasePlan,
    ArtifactEvidence,
    digest_path,
    git_state,
)
from nanolab.release.remote_retry import retry_on_connection_death

_GO_TOOLCHAIN = (
    "golang:1.24-alpine@"
    "sha256:757779acac4af1b349a20f357c7296097b4a0b89da4ad0e370b339060077282a"
)
_NODE_TOOLCHAIN = (
    "node:22-alpine@"
    "sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2"
)
_RUST_TOOLCHAIN = (
    "rust:1.97.1-alpine3.21@"
    "sha256:7bae7c67364dad5ebbd4060923b34d734fbed66d7c1cf3af72aa2f062af93eb6"
)

_SHA256_PREFIX = "sha256:"


def _provider_exec(
    provider: object,
    request: object,
    argv: tuple[str, ...],
    *,
    remote_dir: str | None = None,
    env: dict[str, str] | None = None,
    bounded: bool = False,
) -> object:
    if bounded:
        # The SSH executor waits for the exit status before draining output,
        # so a command whose output exceeds the channel window (~2MB)
        # deadlocks: the remote writer blocks and the command never exits.
        # Bulk commands (tests, image builds, pushes) buffer output remotely
        # and return only a 64KB tail — plenty for diagnosis, and stderr is
        # folded into stdout. Commands whose stdout gets parsed must NOT be
        # bounded.
        script = shlex.join(argv)
        argv = (
            "sh",
            "-c",
            "{ " + script + " ; } >/tmp/release-cmd.log 2>&1; "
            "ec=$?; tail -c 65536 /tmp/release-cmd.log; exit $ec",
        )
    result = retry_on_connection_death(
        lambda: provider.exec_argv(  # type: ignore[attr-defined]
            request, argv, env=env, remote_dir=remote_dir, dry_run=False
        ),
        describe="remote command",
    )
    return _require_result(result, "remote release command")


def _provider_transfer_to(
    provider: object,
    request: object,
    *,
    source: Path,
    destination: str,
    action: str,
) -> object:
    result = retry_on_connection_death(
        lambda: provider.transfer_to(  # type: ignore[attr-defined]
            request, source=source, destination=destination
        ),
        describe=f"transfer {source.name}",
    )
    return _require_result(result, action)


def _require_result(result: object, action: str) -> object:
    return_code = int(getattr(result, "return_code", 0))
    if return_code != 0:
        detail = str(getattr(result, "stderr", "") or getattr(result, "stdout", ""))
        raise RuntimeError(detail or f"{action} failed (exit {return_code})")
    return result


def _assert_guarded_source(plan: Amd64ReleasePlan) -> None:
    source = git_state(plan.repo_root)
    if not source.clean:
        raise ValueError("release requires a clean Git tree")
    if source.commit != plan.identity.source_commit:
        raise ValueError("release source commit changed after planning")


def _write_json(path: Path, payload: Mapping[str, Any]) -> ArtifactEvidence:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return ArtifactEvidence("local", str(path), digest_path(path))


def source_test_commands(remote_source_dir: Path) -> tuple[CommandTaskSpec, ...]:
    """Return the source test commands run against the staged release tree.

    One command per language: the Gradle suite, the Python SDK and functions,
    and the Go, JavaScript, Rust and Bash suites inside digest-pinned
    toolchains. All run in the staged `remote_source_dir`, and the Gradle
    command also collects failure reports and environment diagnostics.
    """
    source = str(remote_source_dir)
    container_prefix = (
        "docker",
        "run",
        "--rm",
        "-v",
        f"{source}:/source:ro",
        "-w",
        "/workspace",
    )
    copy_source = "set -eu; cp -a /source/. /workspace && "
    diagnostics = str(Path(remote_source_dir).parent / "diagnostics")
    diagnostic_script = textwrap.dedent(
        f"""\
        set -uo pipefail

        DIAG={shlex.quote(diagnostics)}
        rm -rf "$DIAG"
        mkdir -p "$DIAG"

        {{
            echo "===== DATE ====="
            date --iso-8601=seconds

            echo
            echo "===== IDENTITY ====="
            id
            hostname
            pwd

            echo
            echo "===== ENVIRONMENT ====="
            env | sort

            echo
            echo "===== LIMITS ====="
            ulimit -a

            echo
            echo "===== SYSTEM ====="
            uname -a
            free -h
            df -h .

            echo
            echo "===== JAVA ====="
            command -v java
            readlink -f "$(command -v java)"
            java -version

            echo
            echo "===== GRADLE ====="
            ./gradlew --version
            ./gradlew -q javaToolchains
        }} >"$DIAG/environment.txt" 2>&1

        set +e

        env \
            -u KUBECONFIG \
            -u NANOFAAS_RUN_K8S_E2E \
            -u NANOFAAS_E2E_NAMESPACE \
            ./gradlew test \
            --no-parallel \
            --console=plain \
            --info \
            --stacktrace \
            2>&1 | tee "$DIAG/gradle.log"

        status=${{PIPESTATUS[0]}}

        if [ "$status" -ne 0 ]; then
            echo "Gradle failed with status $status"

            while IFS= read -r -d '' report; do
                cp --parents "$report" "$DIAG"
            done < <(
                find . \
                    -type f \
                    -path '*/build/test-results/test/TEST-*.xml' \
                    -print0
            )

            python3 - <<'PY' | tee "$DIAG/failures.txt"
        from pathlib import Path
        import xml.etree.ElementTree as ET

        found = False

        for report in sorted(Path(".").glob(
            "**/build/test-results/test/TEST-*.xml"
        )):
            suite = ET.parse(report).getroot()

            for case in suite.findall(".//testcase"):
                problem = case.find("failure")
                kind = "FAILURE"

                if problem is None:
                    problem = case.find("error")
                    kind = "ERROR"

                if problem is None:
                    continue

                found = True
                print("=" * 100)
                print(f"{{kind}}: {{case.get('classname')}}.{{case.get('name')}}")
                print(f"REPORT: {{report}}")
                print(f"MESSAGE: {{problem.get('message', '')}}")
                print()
                print((problem.text or "").strip())

        if not found:
            print("No failure/error elements found in JUnit XML files.")
        PY
        fi

        exit "$status"
        """
    )

    python_argv = (
        "uv",
        "run",
        "--project",
        "sdks/python",
        "--extra",
        "test",
        "--locked",
        "pytest",
        "-q",
        "-o",
        f"cache_dir={remote_source_dir.parent}/source-test-output/pytest-cache",
        "sdks/python/tests",
        "functions/python/word-stats/tests",
        "functions/python/json-transform/tests",
        "functions/python/roman-numeral/tests",
    )
    python_source = remote_source_dir.parent / "source-test-output/python-source"
    # Editable backend metadata belongs in the test workspace, never in the
    # archive used as recipe build context. Keep SDK corpus/function data too.
    python_script = (
        f"set -eu; rm -rf -- {shlex.quote(str(python_source))}; "
        f"mkdir -p {shlex.quote(str(python_source))}; "
        f"cp -a -- {shlex.quote(source + '/sdks')} "
        f"{shlex.quote(source + '/functions')} "
        f"{shlex.quote(source + '/pytest.ini')} {shlex.quote(str(python_source))}/; "
        f"cd {shlex.quote(str(python_source))}; exec {shlex.join(python_argv)}"
    )

    return (
        CommandTaskSpec(
            task_id="release.source.gradle",
            summary="Run Java source tests",
            argv=("bash", "-c", diagnostic_script),
            role="stack",
            options=CommandOptions(remote_dir=source),
        ),
        CommandTaskSpec(
            task_id="release.source.python-sdk",
            summary="Run Python SDK and function source tests",
            argv=("bash", "-c", python_script),
            role="stack",
            options=CommandOptions(
                remote_dir=source,
                env={
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "UV_PROJECT_ENVIRONMENT": str(
                        remote_source_dir.parent / "source-test-output/python-venv"
                    ),
                },
            ),
        ),
        CommandTaskSpec(
            task_id="release.source.go",
            summary="Run Go source tests in pinned toolchain",
            argv=(
                *container_prefix,
                _GO_TOOLCHAIN,
                "sh",
                "-c",
                # The Go adapter shells out to the shared corpus validator,
                # which is a Python script; the pinned toolchain is Alpine.
                copy_source + "apk add --no-cache python3 >/dev/null && "
                "for d in sdks/go functions/go/word-stats "
                "functions/go/json-transform "
                'functions/go/roman-numeral; do (cd "$d" && go test ./...); done',
            ),
            role="stack",
            options=CommandOptions(remote_dir=source),
        ),
        CommandTaskSpec(
            task_id="release.source.node",
            summary="Run JavaScript source tests in pinned toolchain",
            argv=(
                *container_prefix,
                _NODE_TOOLCHAIN,
                "sh",
                "-c",
                # Same shared validator, same reason: node:22-alpine has no python3.
                copy_source + "apk add --no-cache python3 >/dev/null && "
                "npm --prefix sdks/javascript ci && "
                "npm --prefix sdks/javascript test && "
                "for d in functions/javascript/word-stats "
                "functions/javascript/json-transform "
                "functions/javascript/roman-numeral; "
                'do (cd "$d" && npm ci && npm test); done',
            ),
            role="stack",
            options=CommandOptions(remote_dir=source),
        ),
        CommandTaskSpec(
            task_id="release.source.rust",
            summary="Run Rust source tests in pinned toolchain",
            argv=(
                *container_prefix,
                _RUST_TOOLCHAIN,
                "sh",
                "-c",
                copy_source + "apk add --no-cache bash curl jq netcat-openbsd "
                "python3 musl-dev >/dev/null && "
                'export CARGO_TARGET_DIR="$PWD/build/cargo-target" && '
                "for d in sdks/rust functions/rust/word-stats "
                "functions/rust/json-transform functions/rust/roman-numeral "
                'functions/rust/qr-code; do (cd "$d" && cargo test) || exit $?; '
                "done && unset CARGO_TARGET_DIR && "
                "cargo test --manifest-path runtimes/watchdog/Cargo.toml && "
                "bash runtimes/watchdog/test-local.sh",
            ),
            role="stack",
            options=CommandOptions(remote_dir=source),
        ),
        CommandTaskSpec(
            task_id="release.source.bash",
            summary="Run Bash source tests in pinned toolchain",
            argv=(
                *container_prefix,
                _NODE_TOOLCHAIN,
                "sh",
                "-c",
                copy_source + "apk add --no-cache bash jq >/dev/null && "
                "bash functions/bash/roman-numeral/tests/test_handler.sh",
            ),
            role="stack",
            options=CommandOptions(remote_dir=source),
        ),
    )


def extract_commit_tree(
    repo_root: Path,
    commit: str,
    destination: Path,
    *,
    archive_destination: Path | None = None,
) -> Path:
    """Materialize one commit as a plain tree, free of worktree state.

    Planning reads this instead of the checkout, so ignored build output and
    untracked files cannot add phantom targets to the image matrix.
    """
    output = Path(destination)
    archive: Path | None = None
    try:
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            raise ValueError(f"extraction destination is not empty: {output}")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".commit-tree.", suffix=".tar"
        )
        os.close(descriptor)
        archive = Path(temporary_name)
        subprocess.run(
            ("git", "archive", "--format=tar", f"--output={archive}", commit),
            cwd=Path(repo_root),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            (sys.executable, "-c", _EXTRACT_ARCHIVE_SCRIPT, str(archive), str(output)),
            check=True,
            capture_output=True,
        )
        if archive_destination is not None:
            archive_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, archive_destination)
    except (subprocess.CalledProcessError, tarfile.TarError, OSError) as error:
        # The CLI turns ValueError from the preflight into a clean BadParameter;
        # git and tarfile raise neither, so normalize here rather than leaking a
        # traceback out of an offline preflight. mkdir/mkstemp are inside the
        # try too: an OSError from either must not escape unnormalized either.
        raise ValueError(f"could not extract release source for {commit}") from error
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
    return output


def create_source_archive(
    repo_root: Path,
    guarded_commit: str,
    destination: Path,
) -> ArtifactEvidence:
    """Archive one commit as a tar file and return its file evidence.

    Refuses to overwrite an existing archive, and re-checks the Git state
    before and after, so the returned digest always describes the tree that
    was archived.
    """
    root = Path(repo_root)
    before = git_state(root)
    if not before.clean:
        raise ValueError("release requires a clean Git tree")
    if before.commit != guarded_commit:
        raise ValueError("release source commit changed after planning")
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite source archive: {output}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        subprocess.run(
            ("git", "archive", "--format=tar", f"--output={temporary}", guarded_commit),
            cwd=root,
            check=True,
        )
        after = git_state(root)
        if after != before:
            raise ValueError("release source changed while creating archive")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return ArtifactEvidence("local", str(output), digest_path(output))


@dataclass(frozen=True)
class _ArchiveProvider:
    """Keep release connection retries while supplying Sonata's existing port."""

    provider: object

    def exec_argv(
        self, request: object, argv: tuple[str, ...]
    ) -> RemoteOperationResult:
        return cast(
            RemoteOperationResult,
            retry_on_connection_death(
                lambda: self.provider.exec_argv(  # type: ignore[attr-defined]
                    request, argv, env=None, remote_dir=None, dry_run=False
                ),
                describe="remote command",
            ),
        )

    def transfer_to(
        self, request: object, *, source: Path, destination: str
    ) -> RemoteOperationResult:
        return cast(
            RemoteOperationResult,
            retry_on_connection_death(
                lambda: self.provider.transfer_to(  # type: ignore[attr-defined]
                    request, source=source, destination=destination
                ),
                describe=f"transfer {source.name}",
            ),
        )


def stage_source_archive(
    provider: object,
    request: object,
    *,
    archive: Path,
    remote_archive: str,
    remote_source_dir: str,
    expected_digest: str | None = None,
) -> None:
    """Stage the guarded release archive through Sonata's verified transfer."""
    _stage_source_archive(
        _ArchiveProvider(provider),
        request,
        archive=archive,
        remote_archive=remote_archive,
        remote_source_dir=remote_source_dir,
        expected_digest=expected_digest,
    )


def _smoke_arm64_images(
    plan: Amd64ReleasePlan,
    image_plan: ImagePlan,
    provider: object,
    request: object,
    expected_build_evidence: Iterable[ArtifactEvidence],
    *,
    registry_upstream: str,
    ensure_tunnel: bool = True,
) -> tuple[ArtifactEvidence, ...]:
    _assert_guarded_source(plan)
    if ensure_tunnel:
        _provider_exec(
            provider, request, arm.registry_tunnel_command(registry_upstream)
        )
    expected = tuple(expected_build_evidence)
    arm.require_complete_arm64_evidence(image_plan, expected)
    current = tuple(
        ArtifactEvidence(
            "remote",
            f"docker://{cell.image}",
            _inspect_registry_digest(provider, request, cell.image),
        )
        for cell in image_plan.cells
    )
    if _evidence_map(current) != _evidence_map(expected):
        raise RuntimeError("ARM64 registry evidence changed before smoke")
    digests = {artifact.reference: artifact.digest for artifact in expected}
    checked_servers: list[str] = []
    for smoke in arm.server_smoke_specs(image_plan):
        digest = digests[f"docker://{smoke.cell.image}"]
        _smoke_arm64_server(
            provider, request, smoke, _pinned_image(smoke.cell.image, digest)
        )
        checked_servers.append(smoke.cell.image)

    watchdog = arm.watchdog_cell(image_plan)
    watchdog_digest = digests[f"docker://{watchdog.image}"]
    watchdog_result = provider.exec_argv(  # type: ignore[attr-defined]
        request,
        (
            "docker",
            "run",
            "--rm",
            "--platform",
            arm.ARM64_PLATFORM,
            "--env",
            "WARM=true",
            "--env",
            "WATCHDOG_CMD=/nanofaas-arm64-smoke-missing-child",
            _pinned_image(watchdog.image, watchdog_digest),
        ),
        env=None,
        remote_dir=None,
        dry_run=False,
    )
    arm.require_expected_watchdog_exit(
        int(getattr(watchdog_result, "return_code", 0)),
        str(getattr(watchdog_result, "stdout", "")),
        str(getattr(watchdog_result, "stderr", "")),
    )
    marker = _write_json(
        plan.run_dir / "arm64-smoke.json",
        {
            "architecture": arm.ARM64_PLATFORM,
            "images": {
                cell.image: digests[f"docker://{cell.image}"]
                for cell in image_plan.cells
            },
            "serverHealthChecks": checked_servers,
            "watchdog": {
                "image": watchdog.image,
                "expectedExitCode": 1,
                "expectedFailure": "missing child executable",
            },
        },
    )
    return (marker,)


def _smoke_arm64_server(
    provider: object,
    request: object,
    smoke: arm.ServerSmokeSpec,
    image: str,
) -> None:
    try:
        _provider_exec(
            provider,
            request,
            (
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                smoke.container_name,
                "--platform",
                arm.ARM64_PLATFORM,
                "--publish",
                f"127.0.0.1::{smoke.container_port}",
                image,
            ),
        )
        port = _provider_exec(
            provider,
            request,
            ("docker", "port", smoke.container_name, f"{smoke.container_port}/tcp"),
        )
        endpoint = str(getattr(port, "stdout", "")).strip()
        host, separator, value = endpoint.rpartition(":")
        if host != "127.0.0.1" or separator != ":" or not value.isdigit():
            raise RuntimeError(
                f"invalid ARM64 smoke port mapping: {endpoint or 'empty'}"
            )
        _provider_exec(
            provider,
            request,
            (
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "2",
                "--max-time",
                "2",
                "--retry",
                "59",
                "--retry-delay",
                "2",
                "--retry-all-errors",
                "--retry-max-time",
                "120",
                f"http://{endpoint}{smoke.health_path}",  # NOSONAR (S5332): smoke probe
            ),
        )
    except BaseException:
        with contextlib.suppress(OSError, RuntimeError):
            provider.exec_argv(  # type: ignore[attr-defined]
                request,
                ("docker", "rm", "--force", smoke.container_name),
                env=None,
                remote_dir=None,
                dry_run=False,
            )
        raise
    _provider_exec(
        provider,
        request,
        ("docker", "rm", "--force", smoke.container_name),
    )


def _pinned_image(tagged: str, digest: str) -> str:
    repository, _ = tagged.rsplit(":", 1)
    return f"{repository}@{digest}"


def _evidence_map(
    artifacts: Iterable[ArtifactEvidence],
) -> dict[tuple[str, str], str]:
    return {
        (artifact.location, artifact.reference): artifact.digest
        for artifact in artifacts
    }


def _inspect_image_digest(provider: object, request: object, reference: str) -> str:
    result = _provider_exec(
        provider,
        request,
        ("docker", "image", "inspect", "--format={{.Id}}", reference),
    )
    digest = str(getattr(result, "stdout", "")).strip()
    if not digest.startswith(_SHA256_PREFIX) or len(digest) != 71:
        raise RuntimeError(f"invalid image digest for {reference}")
    return digest


def _remote_image_digest(
    provider: object,
    request: object,
    location: str,
    reference: str,
    *,
    ghcr_authfile: str | None = None,
) -> str | None:
    if location != "remote":
        return None
    try:
        if reference.startswith("docker-daemon:"):
            return _inspect_image_digest(
                provider, request, reference.removeprefix("docker-daemon:")
            )
        if reference.startswith("docker://"):
            registry_reference = reference.removeprefix("docker://")
            if registry_reference.startswith("ghcr.io/"):
                if ghcr_authfile is None:
                    # fail closed: unverifiable GHCR evidence is never reused
                    return None
                return _inspect_ghcr_digest(
                    provider, request, registry_reference, authfile=ghcr_authfile
                )
            return _inspect_registry_digest(provider, request, registry_reference)
        return None
    except Exception:
        return None


def _inspect_ghcr_digest(
    provider: object,
    request: object,
    reference: str,
    *,
    authfile: str,
) -> str:
    result = _provider_exec(
        provider,
        request,
        (
            "skopeo",
            "inspect",
            f"--authfile={authfile}",
            "--format={{.Digest}}",
            f"docker://{reference}",
        ),
    )
    digest = str(getattr(result, "stdout", "")).strip()
    if not digest.startswith(_SHA256_PREFIX) or len(digest) != 71:
        raise RuntimeError(f"invalid registry digest for {reference}")
    return digest


def _inspect_registry_digest(provider: object, request: object, reference: str) -> str:
    result = _provider_exec(
        provider,
        request,
        (
            "skopeo",
            "inspect",
            "--tls-verify=false",
            "--format={{.Digest}}",
            f"docker://{reference}",
        ),
    )
    digest = str(getattr(result, "stdout", "")).strip()
    if not digest.startswith(_SHA256_PREFIX) or len(digest) != 71:
        raise RuntimeError(f"invalid registry digest for {reference}")
    return digest


def _registry_digest_map(
    image_plan: ImagePlan,
    artifacts: Iterable[ArtifactEvidence],
) -> dict[str, str]:
    by_reference = {artifact.reference: artifact.digest for artifact in artifacts}
    expected = {f"docker://{cell.image}" for cell in image_plan.cells}
    if set(by_reference) != expected:
        raise ValueError("local-registry-push evidence does not cover the image matrix")
    return {
        cell.image: by_reference[f"docker://{cell.image}"] for cell in image_plan.cells
    }
