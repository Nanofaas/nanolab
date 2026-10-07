"""Product authentication adapters for private file-based release credentials."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from sonata_tasks.credentials import CredentialCleanupError, stage_private_files
from sonata_tasks.credentials import validate_private_file as validate_secret_file
from sonata_tasks.transfer import RemoteOperationResult


@dataclass(frozen=True)
class RemoteDockerCredentials:
    """The remote `DOCKER_CONFIG` directory holding a staged login."""

    docker_config: str


@dataclass(frozen=True)
class RemoteCosignCredentials:
    """Remote paths of the staged cosign key and optional password file."""

    key_file: str
    password_file: str | None


class ReleaseCredentialCleanupError(RuntimeError):
    """A cleanup failure with safe information about the interrupted operation."""

    def __init__(self, operation_type: str) -> None:
        """Record which operation failed to clean up its staged credentials."""
        self.operation_type = operation_type
        super().__init__(f"release credential cleanup failed after {operation_type}")


def _require_success(result: object, action: str) -> object:
    return_code = getattr(result, "return_code", None)
    if not isinstance(return_code, int) or isinstance(return_code, bool):
        raise RuntimeError(f"{action} returned no integer return_code")
    if return_code != 0:
        raise RuntimeError(f"{action} failed (exit {return_code})")
    return result


def _run(
    provider: object,
    request: object,
    argv: tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
) -> object:
    try:
        result = provider.exec_argv(  # type: ignore[attr-defined]
            request,
            argv,
            env=env,
            remote_dir=None,
            dry_run=False,
        )
    except (OSError, RuntimeError):
        raise RuntimeError("remote credential command failed") from None
    return _require_success(result, "remote credential command")


@dataclass(frozen=True)
class _CredentialProvider:
    """Supply the shared port from the release provider's richer invocation."""

    provider: object

    def exec_argv(
        self, request: object, argv: tuple[str, ...]
    ) -> RemoteOperationResult:
        """Run a staging command without a product login environment."""
        return cast(
            RemoteOperationResult,
            self.provider.exec_argv(  # type: ignore[attr-defined]
                request, argv, env=None, remote_dir=None, dry_run=False
            ),
        )

    def transfer_to(
        self, request: object, *, source: Path, destination: str
    ) -> RemoteOperationResult:
        """Forward a private file transfer to the release provider."""
        return cast(
            RemoteOperationResult,
            self.provider.transfer_to(  # type: ignore[attr-defined]
                request, source=source, destination=destination
            ),
        )


@contextmanager
def _stage_remote_files(
    provider: object,
    request: object,
    files: Mapping[str, Path],
) -> Iterator[tuple[str, dict[str, str]]]:
    validated = {name: validate_secret_file(path) for name, path in files.items()}
    try:
        with stage_private_files(
            _CredentialProvider(provider),
            request,
            validated,
            prefix="nanofaas-release-credentials",
        ) as staged:
            yield staged
    except CredentialCleanupError as error:
        raise ReleaseCredentialCleanupError(error.operation_type) from None


@contextmanager
def stage_ghcr_credentials(
    provider: object,
    request: object,
    *,
    username: str,
    token_file: Path,
    registry: str = "ghcr.io",
) -> Iterator[RemoteDockerCredentials]:
    """Authenticate Docker using a staged token and clean all credential state."""
    with _stage_remote_files(provider, request, {"ghcr-token": token_file}) as (
        remote_dir,
        remote_files,
    ):
        docker_config = f"{remote_dir}/docker"
        _run(provider, request, ("mkdir", "-m", "700", docker_config))
        token_path = remote_files["ghcr-token"]
        _run(
            provider,
            request,
            (
                "sh",
                "-c",
                'exec docker login "$1" --username "$2" --password-stdin < "$3"',
                "nanofaas-release-login",
                registry,
                username,
                token_path,
            ),
            env={"DOCKER_CONFIG": docker_config},
        )
        _run(provider, request, ("rm", "-f", "--", token_path))
        yield RemoteDockerCredentials(docker_config=docker_config)


@contextmanager
def stage_cosign_credentials(
    provider: object,
    request: object,
    *,
    key_file: Path,
    password_file: Path | None = None,
) -> Iterator[RemoteCosignCredentials]:
    """Stage cosign files without materializing their contents as strings."""
    files = {"cosign-key": key_file}
    if password_file is not None:
        files["cosign-password"] = password_file
    with _stage_remote_files(provider, request, files) as (_, remote_files):
        yield RemoteCosignCredentials(
            key_file=remote_files["cosign-key"],
            password_file=remote_files.get("cosign-password"),
        )
