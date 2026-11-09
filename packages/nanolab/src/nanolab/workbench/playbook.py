"""Run one workbench playbook on one host."""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Literal, override

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.ansible import build_ansible_argv

from nanolab.one_shot.infrastructure import NodeResource
from nanolab.one_shot.sites import through_site
from nanolab.tasks.components.bootstrap import ansible_operation
from nanolab.tasks.components.operations import RemoteCommandOperation
from nanolab.tasks.infra.ansible import bundled_ansible_root
from nanolab.tasks.provisioning.bootstrap import (
    retarget_cloud_operations,
    run_bootstrap_operations,
    scenario_context,
)
from nanolab.tasks.vm.models import VmInfo
from nanolab.workbench.inventory import HostVars, write_host_vars


@dataclass(frozen=True)
class WorkbenchHost:
    """Where a playbook runs: a node VM, or the nanolab host when `node` is None."""

    key: str
    node: NodeResource | None


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
        self.vars_dir = vars_dir
        self.collections = collections
        self.vm = vm

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        del inputs
        collections = self.collections
        vars_file = write_host_vars(
            self.vars_dir / f"{self.host.key}-{self.action}.json", self.host_vars
        )
        playbook = (
            collections
            / "ansible_collections/nanofaas/workbench/playbooks"
            / f"{self.action}.yml"
        )
        env = {"ANSIBLE_COLLECTIONS_PATH": str(collections)}
        extra_vars = {"workbench_vars": str(vars_file)}
        node = self.host.node
        if node is None:
            argv = build_ansible_argv(
                playbook=playbook,
                inventory="localhost,",
                user=getpass.getuser(),
                extra_vars=extra_vars,
            )
            operation = RemoteCommandOperation(
                operation_id=f"workbench.{self.action}",
                summary=self.title,
                argv=argv,
                env=MappingProxyType(
                    {
                        "ANSIBLE_CONFIG": str(bundled_ansible_root() / "ansible.cfg"),
                        **env,
                    }
                ),
            )
            run_bootstrap_operations(None, (operation,), role=self.host.key)
            return TaskOutcome()
        vm = self.vm
        if vm is None:
            raise ValueError(f"{self.host.key} needs its VM")
        request = node.request.model_copy(
            update={"host": vm.host, "user": vm.user, "home": vm.home}
        )
        assets = Path(str(files("nanolab").joinpath("assets")))
        # repo_root is only the source of repository syncs, which this never plans.
        context = scenario_context(assets, request, assets)
        operations = retarget_cloud_operations(
            node.provider,
            context,
            (
                ansible_operation(
                    context=context,
                    operation_id=f"workbench.{self.action}",
                    summary=self.title,
                    playbook=playbook,
                    extra_vars=extra_vars,
                    env=env,
                ),
            ),
        )
        if node.site_host is not None:
            operations = through_site(
                operations, context=context, host=vm.host, site=node.site_host.site
            )
        run_bootstrap_operations(node.provider, operations, role=node.config.id)
        return TaskOutcome()
