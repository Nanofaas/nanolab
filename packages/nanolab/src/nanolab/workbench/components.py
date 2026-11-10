"""One Sonata resource per workbench component instance."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome

from nanolab.one_shot.site_cpu import parse_cpu_list
from nanolab.tasks.resource_tasks import release_call
from nanolab.tasks.vm.models import VmInfo
from nanolab.workbench.install import ensure_installed
from nanolab.workbench.inventory import HostVars, Instance
from nanolab.workbench.playbook import WorkbenchHost, WorkbenchPlaybook

Action = Literal["deploy", "collect", "undeploy"]


@dataclass(frozen=True)
class Deployed:
    """What a deployed component instance leaves in the journal.

    `vm` is the host VM's journal form (None on the nanolab host), so an
    undeploy in a later process needs no live resource. `deployed_at` (epoch
    seconds, before deploy starts) opens the windows collect steps export. `config`
    is the part of the instance's config resolved at deploy, which undeploy
    reuses.
    """

    host: str
    instance: str
    component: str
    url: str | None
    vm: dict[str, Any] | None = None
    deployed_at: float | None = None
    config: dict[str, Any] | None = None


def _playbook(
    title: str,
    host: WorkbenchHost,
    action: Action,
    instances: tuple[Instance, ...],
    vars_dir: Path,
    collections: Path,
    vm: VmInfo | None,
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
        collections=collections,
        vm=vm,
    )


def _with_config(instance: Instance, extra: dict[str, Any] | None) -> Instance:
    if not extra:
        return instance
    return instance.model_copy(update={"config": {**instance.config, **extra}})


def _vm(host: WorkbenchHost, inputs: TaskInputs) -> VmInfo | None:
    return None if host.node is None else inputs.resource(host.node.vm)


class _Deploy(Task[Deployed]):
    idempotent = False

    def __init__(
        self,
        title: str,
        host: WorkbenchHost,
        instance: Instance,
        url: str | None,
        vars_dir: Path,
        installed: Resource[Path] | None,
        config_from_inputs: Callable[[TaskInputs], dict[str, Any]] | None,
    ) -> None:
        self.title = title
        self.host = host
        self.instance = instance
        self.url = url
        self.vars_dir = vars_dir
        self.installed = installed
        self.config_from_inputs = config_from_inputs

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[Deployed]:
        vm = _vm(self.host, inputs)
        resolved = (
            None if self.config_from_inputs is None else self.config_from_inputs(inputs)
        )
        instance = _with_config(self.instance, resolved)
        collections = (
            ensure_installed()
            if self.installed is None
            else inputs.resource(self.installed)
        )

        def run(action: Action, title: str) -> None:
            _playbook(
                title, self.host, action, (instance,), self.vars_dir, collections, vm
            ).run(inputs)

        started_at = time.time()
        try:
            run("deploy", self.title)
            if self.host.node is None and instance.cpuset is not None:
                observed = subprocess.run(
                    (
                        "docker",
                        "inspect",
                        "--format={{.HostConfig.CpusetCpus}}",
                        f"wb-{instance.name}",
                    ),
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                ).stdout.strip()
                if parse_cpu_list(observed) != parse_cpu_list(instance.cpuset):
                    raise ValueError(
                        f"CPU placement for {instance.name}: expected "
                        f"{instance.cpuset}, got {observed!r}"
                    )
        except BaseException as error:
            # A failed acquire is never released: leave nothing half deployed.
            try:
                run("undeploy", f"Undeploy {self.instance.name} after a failed deploy")
            except Exception as cleanup:
                error.add_note(f"undeploy after the failed deploy: {cleanup}")
            raise
        return TaskOutcome(
            value=Deployed(
                self.host.key,
                self.instance.name,
                self.instance.component,
                self.url,
                None if vm is None else asdict(vm),
                started_at,
                resolved,
            )
        )


class _Collect(Task[None]):
    idempotent = False

    def __init__(
        self,
        host: WorkbenchHost,
        instances: tuple[Instance, ...],
        collect_dir: Path,
        vars_dir: Path,
        installed: Resource[Path],
    ) -> None:
        self.title = f"Collect {host.key}"
        self.host = host
        self.instances = instances
        self.collect_dir = collect_dir
        self.vars_dir = vars_dir
        self.installed = installed

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return _playbook(
            self.title,
            self.host,
            "collect",
            self.instances,
            self.vars_dir,
            inputs.resource(self.installed),
            _vm(self.host, inputs),
            self.collect_dir,
        ).run(inputs)


def component_resource(
    host: WorkbenchHost,
    instance: Instance,
    *,
    url: str | None,
    vars_dir: Path,
    installed: Resource[Path] | None,
    requires: tuple[Resource[Any], ...] = (),
    config_from_inputs: Callable[[TaskInputs], dict[str, Any]] | None = None,
) -> Resource[Deployed]:
    """Deploy `instance` on `host`; undeploy it on release, even when kept.

    `config_from_inputs` adds config only known once `requires` are acquired;
    it is recorded in the `Deployed`, so undeploy needs no inputs. With
    `installed` None the deploy installs the collection inline, so a task can
    acquire the resource by hand (`acquire_now`) without declaring it.
    """
    where = f"{instance.name} on {host.key}"
    node = () if host.node is None else (host.node.vm,)

    def undeploy(inputs: TaskInputs, value: Deployed) -> None:
        # A teardown in a later process rebuilds this resource by title and
        # runs the release with no resource values, so everything comes from
        # the journalled `Deployed` and the workspace, never from `inputs`.
        revive = None if host.node is None else host.node.vm.revive
        vm = None if revive is None or value.vm is None else revive(value.vm)
        _playbook(
            f"Undeploy {where}",
            host,
            "undeploy",
            (_with_config(instance, value.config),),
            vars_dir,
            ensure_installed(),
            vm,
        ).run(inputs)

    return Resource(
        f"Deploy {where}",
        _Deploy(
            f"Deploy {where}",
            host,
            instance,
            url,
            vars_dir,
            installed,
            config_from_inputs,
        ),
        release_call(f"Undeploy {where}", undeploy),
        requires=(*(() if installed is None else (installed,)), *node, *requires),
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
    return _Collect(host, instances, collect_dir, vars_dir, installed)


def collect_deployed(
    host: WorkbenchHost,
    instance: Instance,
    value: Deployed,
    *,
    vars_dir: Path,
    collect_dir: Path,
) -> WorkbenchPlaybook:
    """Collect using journalled configuration and VM facts, including after restart."""
    revive = None if host.node is None else host.node.vm.revive
    return _playbook(
        f"Collect {instance.name}",
        host,
        "collect",
        (_with_config(instance, value.config),),
        vars_dir,
        ensure_installed(),
        None if revive is None or value.vm is None else revive(value.vm),
        collect_dir,
    )
