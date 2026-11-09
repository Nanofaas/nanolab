"""Locate the Ansible playbooks bundled inside NanoLab."""

from __future__ import annotations

from pathlib import Path

from nanolab.workspace.paths import operator_workspace_root


def bundled_ansible_root() -> Path:
    """Path to the Ansible playbooks bundled inside the library."""
    return Path(__file__).parent / "ansible_assets"


def workbench_collections_path() -> Path:
    """Where the pinned nanofaas.workbench collection is installed."""
    return operator_workspace_root() / ".ansible" / "collections"
