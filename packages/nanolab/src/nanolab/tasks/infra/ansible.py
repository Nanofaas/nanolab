"""Locate the Ansible playbooks bundled inside NanoLab."""

from __future__ import annotations

from pathlib import Path


def bundled_ansible_root() -> Path:
    """Path to the Ansible playbooks bundled inside the library."""
    return Path(__file__).parent / "ansible_assets"
