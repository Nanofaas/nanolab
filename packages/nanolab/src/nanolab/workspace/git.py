"""Git commands run in a checkout, with their output captured."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_output(root: Path, *args: str) -> bytes:
    """Return the stdout of `git *args` run in `root`.

    Raises `subprocess.CalledProcessError` when git fails and `OSError` when it
    cannot run; its stderr goes with the error, not to the terminal.
    """
    return subprocess.run(
        ("git", *args), cwd=root, check=True, capture_output=True, timeout=120
    ).stdout
