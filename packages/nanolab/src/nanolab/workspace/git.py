"""Git commands run in a checkout, with their output captured."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_output(root: Path, *args: str) -> bytes:
    """Return the stdout of `git *args` run in `root`.

    Raises `subprocess.CalledProcessError` on failure, `subprocess.TimeoutExpired`
    after 120 seconds, or `OSError` when Git cannot run. Captured stderr stays
    with the error, not on the terminal.
    """
    return subprocess.run(
        ("git", *args), cwd=root, check=True, capture_output=True, timeout=120
    ).stdout
