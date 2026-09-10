"""Shared host prerequisite diagnostics."""

import shutil

REQUIRED_EXECUTABLES = ("docker", "ssh")


def missing_executables(required: tuple[str, ...] = REQUIRED_EXECUTABLES) -> list[str]:
    """Return the names in `required` that are not on the host's PATH."""
    return [name for name in required if shutil.which(name) is None]
