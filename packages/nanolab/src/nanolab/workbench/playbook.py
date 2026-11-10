"""Run one workbench playbook on one host."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, override

from sonata_engine import Task, TaskInputs, TaskOutcome

from nanolab.one_shot.infrastructure import NodeResource
from nanolab.tasks.infra.ansible import playbook_task
from nanolab.tasks.vm.models import VmInfo
from nanolab.workbench.inventory import HostVars, write_host_vars


@dataclass(frozen=True)
class WorkbenchHost:
    """Where a playbook runs: a node VM, or the nanolab host when `node` is None."""

    key: str
    node: NodeResource | None

    def __post_init__(self) -> None:
        """Keep inventory aliases safe as evidence path segments."""
        if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", self.key) is None:
            raise ValueError(f"invalid workbench host alias: {self.key!r}")


class WorkbenchPlaybook(Task[None]):
    """Write the host's vars file and run `<action>.yml` of the installed collection."""

    idempotent = False

    def __init__(
        self,
        title: str,
        host: WorkbenchHost,
        action: Literal["deploy", "collect", "undeploy"],
        host_vars: HostVars,
        *,
        vars_dir: Path,
        collections: Path,
        vm: VmInfo | None,
    ) -> None:
        """Bind the task to one host, action and vars."""
        self.title = title
        self.host = host
        self.action = action
        self.host_vars = host_vars
        self.vars_dir = vars_dir.resolve()
        self.collections = collections
        self.vm = vm

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        collections = self.collections
        vars_file = write_host_vars(
            self.vars_dir / f"{self.host.key}-{self.action}.json", self.host_vars
        )
        playbook = (
            collections
            / "ansible_collections/nanofaas/workbench/playbooks"
            / f"{self.action}.yml"
        )
        node = self.host.node
        if node is None:
            # Keep the public collect path's existing localhost segment.
            hosts = {
                "localhost": {
                    "ansible_connection": "local",
                    "ansible_python_interpreter": sys.executable,
                }
            }
        else:
            if self.vm is None:
                raise ValueError(f"{self.host.key} needs its VM")
            hosts = {self.host.key: node.ansible_vars(self.vm)}
        playbook_task(
            title=self.title,
            playbook=playbook,
            inventory=self.vars_dir / f"{self.host.key}-{self.action}-inventory.json",
            hosts=hosts,
            extra_vars={"workbench_vars": str(vars_file)},
            shell=None if node is None else getattr(node.provider, "shell", None),
            env={"ANSIBLE_COLLECTIONS_PATH": str(collections)},
        ).run(inputs)
        return TaskOutcome()
