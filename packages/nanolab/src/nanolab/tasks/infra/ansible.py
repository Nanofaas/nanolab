"""Locate the Ansible playbooks bundled inside NanoLab."""

from __future__ import annotations

import getpass
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sonata_tasks.ansible import AnsiblePlaybookTask
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.shell import SubprocessShell
from sonata_tasks.tasks.executors import HostCommandRunner, HostCommandTaskExecutor

from nanolab.workspace.paths import operator_workspace_root


def bundled_ansible_root() -> Path:
    """Path to the Ansible playbooks bundled inside the library."""
    return Path(__file__).parent / "ansible_assets"


def workbench_collections_path() -> Path:
    """Where the pinned nanofaas.workbench collection is installed."""
    return operator_workspace_root() / ".ansible" / "collections"


# Concurrent ssh processes can mark inherited stdin non-blocking; Ansible refuses it.
OWN_STDIN = ("sh", "-c", 'exec "$@" </dev/null', "sh")


def playbook_task(
    *,
    title: str,
    playbook: Path,
    inventory: Path,
    hosts: Mapping[str, Mapping[str, Any]],
    extra_vars: Mapping[str, str],
    runner: HostCommandRunner | None = None,
    env: Mapping[str, str] | None = None,
) -> AnsiblePlaybookTask:
    """Run a playbook with resolved connection facts and its own stdin."""
    inventory = inventory.resolve()
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(json.dumps({"all": {"hosts": hosts}}, indent=2) + "\n")
    task = AnsiblePlaybookTask(
        title=title,
        playbook=playbook,
        inventory=str(inventory),
        user=getpass.getuser(),
        executor=HostCommandTaskExecutor(runner or SubprocessShell()),
        extra_vars=extra_vars,
        ansible_config=bundled_ansible_root() / "ansible.cfg",
        options=CommandOptions(env=dict(env or {}), remote_dir=None),
    )
    # AnsiblePlaybookTask builds a static command; narrow its inherited union type.
    assert not callable(task.argv)  # nosec B101
    task.argv = (
        *OWN_STDIN,
        sys.executable,
        "-m",
        "ansible.cli.playbook",
        *task.argv[1:],
    )
    return task
