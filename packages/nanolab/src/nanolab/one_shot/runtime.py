"""Ansible runtime lifecycle using the run's persisted connection facts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from sonata_engine import TaskInputs

from nanolab.one_shot.infrastructure import NodeResource
from nanolab.tasks.infra.ansible import bundled_ansible_root, playbook_task
from nanolab.tasks.vm.models import VmInfo
from nanolab.workbench.install import ensure_installed


def run_runtime(
    node: NodeResource,
    run_dir: Path,
    action: Literal["deploy", "undeploy"],
    inputs: TaskInputs,
    *,
    vm: VmInfo | None = None,
    variables: dict[str, Any] | None = None,
) -> None:
    """Deploy with acquired facts, or compensate/release with their saved copy."""
    run_dir = run_dir.resolve()
    directory = run_dir / "ansible"
    inventory = directory / f"{node.config.id}-runtime-inventory.json"
    if action == "undeploy" and not inventory.exists():
        return  # Acquisition failed before any runtime command was submitted.
    if vm is None:
        hosts = json.loads(inventory.read_text())["all"]["hosts"]
    else:
        hosts = {node.config.id: node.ansible_vars(vm)}
    directory.mkdir(parents=True, exist_ok=True)
    values = directory / f"{node.config.id}-runtime-{action}.json"
    values.write_text(
        json.dumps(
            {
                **(variables or {}),
                "runtime_action": action,
                "runtime_name": "nanolab-one-shot-control-plane",
                "runtime_owner": str(run_dir),
                "runtime_log_file": str(
                    run_dir / f"{node.config.id}-control-plane.log"
                ),
            },
            indent=2,
        )
        + "\n"
    )
    playbook_task(
        title=f"{action.capitalize()} {node.config.id} control plane",
        playbook=bundled_ansible_root() / "playbooks/one-shot-runtime.yml",
        inventory=inventory,
        hosts=hosts,
        extra_vars={"runtime_vars": str(values)},
        shell=getattr(node.provider, "shell", None),
        env={"ANSIBLE_COLLECTIONS_PATH": str(ensure_installed())},
    ).run(inputs)
