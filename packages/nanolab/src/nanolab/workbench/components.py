"""One Sonata resource per workbench component instance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome

from nanolab.tasks.resource_tasks import release_call
from nanolab.workbench.inventory import HostVars, Instance
from nanolab.workbench.playbook import WorkbenchHost, WorkbenchPlaybook


@dataclass(frozen=True)
class Deployed:
    """What a deployed component instance leaves in the journal."""

    host: str
    instance: str
    component: str
    url: str | None


class _Deploy(Task[Deployed]):
    idempotent = False

    def __init__(
        self,
        host: WorkbenchHost,
        instance: Instance,
        url: str | None,
        playbook: WorkbenchPlaybook,
    ) -> None:
        self.title = playbook.title
        self.host = host
        self.instance = instance
        self.url = url
        self.playbook = playbook

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[Deployed]:
        self.playbook.run(inputs)
        return TaskOutcome(
            value=Deployed(
                self.host.key, self.instance.name, self.instance.component, self.url
            )
        )


def _playbook(
    title: str,
    host: WorkbenchHost,
    action: Any,
    instances: tuple[Instance, ...],
    vars_dir: Path,
    installed: Resource[Path],
    collect_dir: Path | None = None,
) -> WorkbenchPlaybook:
    return WorkbenchPlaybook(
        title,
        host,
        action,
        HostVars(
            instances=instances,
            collect_dir=None if collect_dir is None else str(collect_dir),
        ),
        vars_dir=vars_dir,
        installed=installed,
    )


def component_resource(
    host: WorkbenchHost,
    instance: Instance,
    *,
    url: str | None,
    vars_dir: Path,
    installed: Resource[Path],
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[Deployed]:
    """Deploy `instance` on `host`; undeploy it on release, even when kept."""
    where = f"{instance.name} on {host.key}"
    undeploy = _playbook(
        f"Undeploy {where}", host, "undeploy", (instance,), vars_dir, installed
    )
    node = () if host.node is None else (host.node.vm,)
    return Resource(
        f"Deploy {where}",
        _Deploy(
            host,
            instance,
            url,
            _playbook(
                f"Deploy {where}", host, "deploy", (instance,), vars_dir, installed
            ),
        ),
        # The undeploy is rebuilt from what was captured here, so a later
        # process releasing from the journal needs only the revived Deployed.
        release_call(undeploy.title, lambda inputs, _value: undeploy.run(inputs)),
        requires=(installed, *node, *requires),
        always_release=True,
        revive=lambda raw: Deployed(**raw),
    )


def collect_task(
    host: WorkbenchHost,
    instances: tuple[Instance, ...],
    collect_dir: Path,
    vars_dir: Path,
    installed: Resource[Path],
) -> Task[None]:
    """Run the `collect` playbook for `instances` into `collect_dir`."""
    return _playbook(
        f"Collect {host.key}",
        host,
        "collect",
        instances,
        vars_dir,
        installed,
        collect_dir,
    )
