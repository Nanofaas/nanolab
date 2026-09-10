"""Command builders for mirroring the repository into a VM over rsync/ssh."""

from __future__ import annotations

from pathlib import Path

from sonata_tasks.vm.ssh import ssh_command

REPO_SYNC_GITIGNORE_FILTER = ":- .gitignore"
REPO_SYNC_EXCLUDE_PATTERNS = (
    ".git",
    ".git/",
    ".gitnexus",
    ".DS_Store",
    ".idea/",
    ".vscode/",
    ".worktrees/",
    "docs/experiments/*/raw/",
    "docs/experiments/*/*/raw/",
)


def repo_sync_ssh_rsh(
    private_key_path: Path | None = None, *, port: int | None = None
) -> str:
    """Return the SSH command rsync should use via `-e` to reach the VM."""
    return ssh_command(private_key_path=private_key_path, port=port)


def repo_rsync_command(
    *, source: Path, user: str, host: str, destination: str, ssh_rsh: str | None = None
) -> list[str]:
    """Build the rsync argv mirroring `source` into `destination` on the VM."""
    command = [
        "rsync",
        "-az",
        "--delete",
        "--delete-excluded",
        "--filter",
        REPO_SYNC_GITIGNORE_FILTER,
        *(f"--exclude={pattern}" for pattern in REPO_SYNC_EXCLUDE_PATTERNS),
    ]
    if ssh_rsh is not None:
        command.extend(("-e", ssh_rsh))
    command.extend((f"{source}/", f"{user}@{host}:{destination.rstrip('/')}/"))
    return command
