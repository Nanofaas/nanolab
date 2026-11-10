"""Shared container deployment composition for the three independent workflows."""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx
from sonata_engine import Resource, TaskInputs, Workflow
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from nanolab.application.execution import build_role_bindings
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.one_shot import (
    SCIENTIFIC_CONTROL_PLANE_CPUS,
    SCIENTIFIC_CONTROL_PLANE_MEMORY_MIB,
    OneShotFunction,
    operational_limit,
    resolve_node_functions,
)
from nanolab.functions.catalog import resolve_function_definition
from nanolab.one_shot.addresses import (
    advertised_arguments,
    published,
    seed_arguments,
)
from nanolab.one_shot.infrastructure import (
    NodeExecutor,
    NodeResource,
    OneShotResources,
    build_one_shot_resources,
    machine_architecture,
    transfer_to_vm,
)
from nanolab.one_shot.sites import through_site
from nanolab.one_shot.topology import operator_peer_modes, validate_neighbor_graph
from nanolab.one_shot.vm_resources import require_resource_overhead
from nanolab.tasks.components.bootstrap import plan_vm_provision_base
from nanolab.tasks.one_shot.images import archive_image_ids
from nanolab.tasks.one_shot.operator_logging import log_routes
from nanolab.tasks.one_shot.resources import node_probe_resource
from nanolab.tasks.one_shot.workload import WORKLOAD_NAMES
from nanolab.tasks.platform import (
    Platform,
    PlatformFunction,
    PlatformRequest,
    add_platform,
)
from nanolab.tasks.provisioning.bootstrap import (
    retarget_cloud_operations,
    run_bootstrap_operations,
    scenario_context,
)
from nanolab.tasks.recipes.workflow import (
    RecipeBinding,
    RecipeDistribution,
    assembled_recipe_distribution_resource,
    read_distribution,
    recipe_run_resource,
    require_validation_distribution,
)
from nanolab.tasks.resource_tasks import call, release_call, resource_of
from nanolab.workbench.platform import OneShotPlatform, one_shot_platform
from nanolab.workbench.supervisor import SupervisorClient


@dataclass(frozen=True)
class OneShotTopology:
    """Resource handles; connection facts only exist after Sonata acquires them."""

    resources: OneShotResources
    platforms: dict[str, Platform]
    endpoints: dict[str, Resource[str]]
    probes: dict[str, Resource[str]]
    distribution: Resource[RecipeDistribution]
    function_settings: dict[str, OneShotFunction]
    function_aliases: dict[str, str]
    # The run's workbench platform; `platform.collectors` are its log collectors.
    platform: OneShotPlatform | None = None
    # Attaches the workbench supervisor to the run's supervision, if any.
    supervised: Resource[None] | None = None
    node_function_settings: dict[str, dict[str, OneShotFunction]] = field(
        default_factory=dict
    )
    neighbor_graph: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Each node's pinned distribution when images are per architecture;
    # empty when every node shares `distribution`.
    node_distributions: dict[str, Resource[RecipeDistribution]] = field(
        default_factory=dict
    )

    def distribution_of(self, node_id: str) -> Resource[RecipeDistribution]:
        """Return the pinned distribution a node's functions run from."""
        return self.node_distributions.get(node_id, self.distribution)

    @property
    def requires(self) -> tuple[Resource[Any], ...]:
        """Keep every node, platform and probe alive through the consumer."""
        return (
            *self.resources.vms,
            self.resources.generator,
            *self.endpoints.values(),
            *self.probes.values(),
            self.distribution,
            *self.node_distributions.values(),
            *((self.platform.resource,) if self.platform is not None else ()),
            *((self.supervised,) if self.supervised is not None else ()),
            *(
                function
                for platform in self.platforms.values()
                for function in platform.functions
            ),
        )


# The supervisor runs on the operator's loopback: unreachable this long, it is
# down, not slow.
SUPERVISOR_UNREACHABLE_SECONDS = 30


def workbench_supervision(
    platform: OneShotPlatform, supervision: Any, *, requires: tuple[Resource[Any], ...]
) -> Resource[None]:
    """Poll the platform's supervisor and react to its targets while held.

    Requiring the control planes, it attaches once they run and detaches
    before any of them, or the platform, is stopped on purpose.
    """

    def unwatch() -> None:
        for name in platform.targets:
            supervision.unwatch(name)
        supervision.unwatch("workbench")

    def detach(inputs: TaskInputs, _value: None) -> None:
        # Recorded while every target is still there; on failure the
        # platform's own status step tries again.
        with contextlib.suppress(Exception):
            platform.record_supervisor_status(inputs.resource(platform.resource))
        unwatch()

    def attach(inputs: TaskInputs) -> None:
        url = inputs.resource(platform.resource).of(platform.supervisor).url
        supervision.attach(
            SupervisorClient(url or "", timeout_seconds=min(supervision.cadence, 2.0)),
            tolerated_seconds=SUPERVISOR_UNREACHABLE_SECONDS,
        )
        try:
            # Only watched targets count; re-putting one already running
            # keeps its state, the control planes' start their grace now.
            for name, target in platform.targets.items():
                supervision.watch_remote(
                    name,
                    target["kind"],
                    target["params"],
                    tolerated_seconds=target["toleratedSeconds"],
                )
        except BaseException:
            unwatch()
            raise

    return resource_of(
        "Attach the workbench supervisor",
        attach,
        detach,
        requires=(platform.resource, *requires),
    )


def _bootstrap(
    node: NodeResource, root: Path, *, run_dir: Path | None = None, settings=None
) -> Resource[None]:
    def acquire(inputs: TaskInputs) -> None:
        vm = inputs.resource(node.vm)
        ready = subprocess.run(
            node.vm_argv(
                vm, "sudo", "cloud-init", "status", "--wait", "--format", "json"
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=operational_limit(settings, "cloud_init_seconds", 600),
        )
        if run_dir is not None:
            (run_dir / f"{node.config.id}-cloud-init.json").write_text(
                json.dumps(
                    {
                        "returncode": ready.returncode,
                        "stdout": ready.stdout,
                        "stderr": ready.stderr,
                    }
                )
                + "\n"
            )
        if ready.returncode != 0 or json.loads(ready.stdout).get("status") != "done":
            raise RuntimeError(f"{node.config.id}: cloud-init is not complete")
        request = node.request.model_copy(
            update={"host": vm.host, "user": vm.user, "home": vm.home}
        )
        assets = Path(str(files("nanolab").joinpath("assets")))
        context = scenario_context(root, request, assets)
        operations = retarget_cloud_operations(
            node.provider, context, plan_vm_provision_base(context)
        )
        if node.site_host is not None:
            operations = through_site(
                operations, context=context, host=vm.host, site=node.site_host.site
            )
        run_bootstrap_operations(node.provider, operations, role=node.config.id)
        node.reset_shared_ssh(vm)

    return resource_of(
        f"Bootstrap {node.config.id} container VM",
        acquire,
        lambda _inputs, _value: None,
        requires=(node.vm,),
    )


def sync_queue_arguments(settings) -> list[str]:
    """Override the sync-queue admission limits only when the scenario sets them."""
    limits = getattr(settings, "sync_queue", None)
    if limits is None:
        return []
    return [
        f"--sync-queue.max-estimated-wait={limits.max_estimated_wait_seconds}s",
        f"--sync-queue.max-queue-wait={limits.max_queue_wait_seconds}s",
    ]


def _control_plane(
    node: NodeResource,
    topology: OneShotResources,
    *,
    bootstrap: Resource[None],
    distribution: Resource[RecipeDistribution],
    archive: Resource[Path],
    run_dir: Path,
    forecast_provider: str = "ORACLE",
    terminal_queueing: bool = False,
    baseline_depth: int | None = None,
    platform: Resource | None = None,
    logs_enabled: bool = False,
    neighbor_graph: dict[str, tuple[str, ...]] | None = None,
    scientific: bool = False,
    strict_memory: bool = False,
    forecast_max_age_seconds: int | None = None,
    forecast_window_seconds: int = 1,
    forecast_properties: Mapping[str, str] | None = None,
    settings=None,
    architecture: str | None = None,
) -> Resource[str]:
    name = "nanolab-one-shot-control-plane"

    def release(_inputs: TaskInputs, _url: str | None = None) -> None:
        log = node.provider.exec_argv(node.request, ("docker", "logs", name))
        (run_dir / f"{node.config.id}-control-plane.log").write_text(
            log.stdout + log.stderr
        )
        removed = node.provider.exec_argv(node.request, ("docker", "rm", "-f", name))
        if removed.return_code != 0 and "No such container" not in removed.stderr:
            raise RuntimeError("one-shot control-plane cleanup unconfirmed")

    def acquire(inputs: TaskInputs) -> str:
        vm = inputs.resource(node.vm)
        addresses = node.addresses(vm)
        if architecture is not None:
            machine = node.command(("uname", "-m")).stdout.strip()
            if machine_architecture(machine) != architecture:
                raise ValueError(
                    f"{node.config.id}: VM is {machine}, images are {architecture}"
                )
        built = inputs.resource(distribution)
        image_archive = inputs.resource(archive)
        remote = f"{vm.home}/one-shot-images.tar"
        transfer_to_vm(node, vm, image_archive, remote)
        node.command(("docker", "load", "--input", remote))
        identities = archive_image_ids(
            image_archive,
            {
                component.image.reference: component.image.id
                for component in built.components
            },
        )
        loaded_ids = {}
        for component in built.components:
            actual = node.command(
                (
                    "docker",
                    "image",
                    "inspect",
                    "--format={{.Id}}",
                    component.image.reference,
                )
            ).stdout.strip()
            if actual not in identities[component.image.reference]:
                raise ValueError("loaded image digest mismatch")
            loaded_ids[component.image.reference] = actual
        socket_group = node.command(
            ("stat", "-c", "%g", "/var/run/docker.sock")
        ).stdout.strip()
        if not socket_group.isdigit():
            raise ValueError("invalid Docker socket group")
        edge = node.config.kind == "edge"
        peer_mount = []
        if edge and neighbor_graph is not None:
            operator_file = run_dir / f"{node.config.id}-p2p-operator.yaml"
            operator_file.write_text(
                json.dumps(
                    {
                        "config": {
                            "peers": [
                                {"id": peer, "mode": mode}
                                for peer, mode in operator_peer_modes(
                                    node.config.id, neighbor_graph
                                ).items()
                            ]
                        }
                    },
                    indent=2,
                )
                + "\n"
            )
            remote_dir = f"{vm.home}/one-shot-p2p"
            node.command(("mkdir", "-p", remote_dir))
            transfer_to_vm(node, vm, operator_file, remote_dir + "/peers.yaml")
            # The nonroot platform must atomically replace its state file; this
            # directory exists only inside the run-owned VM.
            node.command(("chmod", "777", remote_dir))
            node.command(("chmod", "666", remote_dir + "/peers.yaml"))
            peer_mount = ["-v", remote_dir + ":/tmp/one-shot-p2p"]
        boolean = str(edge).lower()
        args = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "host",
            "--group-add",
            socket_group,
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-e",
            "DOCKER_HOST=unix:///var/run/docker.sock",
            *(
                [
                    "--log-driver",
                    "json-file",
                    "--log-opt",
                    "max-size=10m",
                    "--log-opt",
                    "max-file=3",
                    "-e",
                    "NANOFAAS_LOG_FORMAT=json",
                ]
                if logs_enabled
                else []
            ),
            *(
                [
                    "--cpus",
                    str(SCIENTIFIC_CONTROL_PLANE_CPUS),
                    "--memory",
                    f"{SCIENTIFIC_CONTROL_PLANE_MEMORY_MIB}m",
                ]
                if scientific
                else []
            ),
            *peer_mount,
            loaded_ids[built.control_plane().image.reference],
            *(
                ["--nanofaas.p2p.state-file=/tmp/one-shot-p2p/peers.yaml"]
                if peer_mount
                else []
            ),
            "--server.port=8080",
            "--management.server.port=9090",
            "--logging.level.root=WARN",
            *(
                [
                    "--logging.level.it.unimib.datai.nanofaas.execution.QueueObservation=INFO"
                ]
                if logs_enabled
                else []
            ),
            "--nanofaas.admission.profile=SYNC_QUEUE",
            *sync_queue_arguments(settings),
            "--nanofaas.metrics.profile=advanced",
            "--nanofaas.registry.path=/tmp/functions.json",
            "--nanofaas.container-local.runtime-adapter=docker-java",
            *(
                ["--nanofaas.container-local.strict-memory=true"]
                if strict_memory
                else []
            ),
            "--nanofaas.container-local.bind-host=0.0.0.0",
            f"--nanofaas.container-local.namespace={vm.name}",
            f"--nanofaas.p2p.enabled={boolean}",
            f"--nanofaas.p2p.admin.enabled={boolean}",
            f"--nanofaas.p2p.node-id={node.config.id}",
            *advertised_arguments(addresses),
            "--nanofaas.p2p.ping-interval=250ms",
            "--nanofaas.p2p.ping-timeout=1s",
            f"--nanofaas.forecasting.enabled={boolean}",
            f"--nanofaas.forecasting.node-id={node.config.id}",
            f"--nanofaas.forecasting.provider={forecast_provider}",
            f"--nanofaas.forecasting.window={forecast_window_seconds}s",
            *(
                f"--nanofaas.forecasting.{key}={value}"
                for key, value in (forecast_properties or {}).items()
            ),
            *(
                [f"--nanofaas.forecasting.max-age={forecast_max_age_seconds}s"]
                if forecast_max_age_seconds is not None
                else []
            ),
            f"--nanofaas.offload.one-shot.enabled={boolean}",
            "--sync-queue.enabled="
            + str(terminal_queueing or (edge and baseline_depth is not None)).lower(),
        ]
        if edge and baseline_depth is not None:
            cloud = next(
                other
                for other in topology.nodes.values()
                if other.config.kind == "cloud"
            )
            cloud_api = cloud.addresses(inputs.resource(cloud.vm)).api
            args.extend(
                (
                    f"--nanofaas.offload.target-url={cloud_api}",
                    f"--sync-queue.max-depth={baseline_depth}",
                )
            )
        if edge:
            args.extend(
                seed_arguments(
                    [
                        other.addresses(inputs.resource(other.vm))
                        for other in topology.nodes.values()
                        if other.config.kind == "edge" and other is not node
                    ]
                )
            )
        node.command(tuple(args))
        deadline = time.monotonic() + operational_limit(
            settings, "service_start_seconds", 90
        )
        with httpx.Client(trust_env=False) as http:
            while time.monotonic() < deadline:
                try:
                    http.get(
                        f"{addresses.management}/actuator/health/readiness",
                        timeout=operational_limit(
                            settings, "collection_http_seconds", 2
                        ),
                    ).raise_for_status()
                    http.get(
                        f"{addresses.api}/v1/functions",
                        timeout=operational_limit(
                            settings, "collection_http_seconds", 2
                        ),
                    ).raise_for_status()
                    return addresses.api
                except httpx.HTTPError:
                    time.sleep(0.2)
        raise RuntimeError(f"{node.config.id} control-plane readiness timed out")

    title = f"Acquire {node.config.id} control plane"
    return compensated_resource(
        title=title,
        acquire=call(title, acquire),
        release=release_call(title, release),
        compensate=call(f"Compensate {title}", release),
        requires=(
            bootstrap,
            distribution,
            archive,
            *topology.vms,
            # The platform's collector is up before, and drains after, it.
            *((platform,) if platform is not None else ()),
        ),
    )


REQUIRED_MODULES = frozenset(
    {
        "offload",
        "forecasting",
        "p2p-discovery",
        "container-deployment-provider",
        "sync-queue",
    }
)


def _architecture_images(
    settings,
    *,
    recipe: Path,
    selected,
    run_dir: Path,
    architectures: set[str],
) -> tuple[dict, dict]:
    """Reuse one verified distribution and image archive per architecture.

    The archive `images-<arch>.tar` sits beside its `distribution.json`; its
    config digests must be the distribution's image IDs, so the operator needs
    no Docker of its own.
    """
    distributions: dict[str | None, Resource[RecipeDistribution]] = {}
    archives: dict[str | None, Resource[Path]] = {}
    for architecture in sorted(architectures):
        reference = settings.runtime_distributions.get(architecture)
        if reference is None:
            raise ValueError(f"no runtime distribution for {architecture} nodes")
        tag = str(json.loads(reference.read_verified())["tag"])
        checked = read_distribution(
            reference.path, recipe=recipe, tag=tag, published=False
        )
        require_validation_distribution(
            checked, functions=selected, required_modules=REQUIRED_MODULES
        )
        distributions[architecture] = resource_of(
            f"Reuse immutable {architecture} one-shot runtime distribution",
            lambda _inputs, checked=checked: checked,
            lambda _inputs, _value: None,
        )

        def verify(
            _inputs,
            *,
            architecture=architecture,
            checked=checked,
            path=reference.path.parent / f"images-{architecture}.tar",
        ) -> Path:
            archive_image_ids(
                path,
                {c.image.reference: c.image.id for c in checked.components},
            )
            (run_dir / f"runtime-distribution-{architecture}.json").write_bytes(
                checked.report.read_bytes()
            )
            return path

        archives[architecture] = resource_of(
            f"Verify prebuilt {architecture} one-shot runtime images",
            verify,
            lambda _inputs, _value: None,
            requires=(distributions[architecture],),
        )
    return distributions, archives


def add_one_shot_platforms(
    workflow: Workflow,
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    run_dir: Path,
    repo_root: Path,
    forecast_provider: str = "ORACLE",
    terminal_queueing: bool = False,
    max_retries: int = 0,
    baseline: bool = False,
    observer_bootstrap: bool = False,
    forecast_max_age_seconds: int | None = None,
    forecast_window_seconds: int = 1,
    forecast_properties: Mapping[str, str] | None = None,
    supervision=None,
) -> OneShotTopology:
    """Compose existing staged recipes, bootstrap and function resources per VM."""
    settings = config.one_shot
    if settings is None:
        raise ValueError("one-shot configuration missing")

    if not observer_bootstrap:
        require_resource_overhead(settings)
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    graph = validate_neighbor_graph(settings)
    resources = build_one_shot_resources(
        settings, environment, repo_root=repo_root, run_dir=run_dir
    )
    definitions = {
        key: resolve_function_definition(key, repo_root) for key in config.functions
    }
    selected = tuple(
        (definition.family, definition.runtime) for definition in definitions.values()
    )
    default_recipe = (
        "one-shot-multifunction-jvm.yaml"
        if settings.strict_memory
        and {definition.family for definition in definitions.values()} == WORKLOAD_NAMES
        else "one-shot-jvm.yaml"
    )
    recipe = config.recipe_profile or Path(
        str(files("nanolab").joinpath("assets", "presets", "recipes", default_recipe))
    )
    if config.control_plane_runtime != "jvm" and config.recipe_profile is None:
        raise ValueError("native one-shot requires an explicit native recipeProfile")
    executor = RoleBoundCommandTaskExecutor(
        build_role_bindings(EnvironmentConfig(provider="local"))[0]
    )
    # Prebuilt images per architecture: each node gets the distribution,
    # archive, pinned identities and function binding of its own architecture.
    if settings.runtime_distributions:
        distributions, archives = _architecture_images(
            settings,
            recipe=recipe,
            selected=selected,
            run_dir=run_dir,
            architectures={node.architecture for node in resources.nodes.values()},
        )
    else:
        if settings.runtime_distribution is None:
            staged = recipe_run_resource(
                source=repo_root,
                recipe=recipe,
                run_dir=run_dir / "build",
                tag="one-shot-local",
            )
            distribution = assembled_recipe_distribution_resource(
                run=staged,
                executor=executor,
                functions=selected,
                required_modules=frozenset(
                    {
                        "offload",
                        "forecasting",
                        "p2p-discovery",
                        "container-deployment-provider",
                        "sync-queue",
                    }
                ),
            )
        else:
            raw = settings.runtime_distribution.read_verified()
            tag = str(json.loads(raw)["tag"])
            checked = read_distribution(
                settings.runtime_distribution.path,
                recipe=recipe,
                tag=tag,
                published=False,
            )
            require_validation_distribution(
                checked,
                functions=selected,
                required_modules=frozenset(
                    {
                        "offload",
                        "forecasting",
                        "p2p-discovery",
                        "container-deployment-provider",
                        "sync-queue",
                    }
                ),
            )
            distribution = resource_of(
                "Reuse immutable one-shot runtime distribution",
                lambda _inputs: checked,
                lambda _inputs, _value: None,
            )

        def save_images(inputs: TaskInputs) -> Path:
            built = inputs.resource(distribution)
            path = run_dir / "images.tar"
            for component in built.components:
                observed = (
                    subprocess.run(
                        (
                            "docker",
                            "image",
                            "inspect",
                            "--format={{.Id}}",
                            component.image.reference,
                        ),
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                    .stdout.decode()
                    .strip()
                )
                if observed != component.image.id:
                    raise ValueError("local image changed since runtime build")
            partial = path.with_suffix(".tar.partial")
            try:
                subprocess.run(
                    (
                        "docker",
                        "save",
                        "--output",
                        str(partial),
                        *(component.image.reference for component in built.components),
                    ),
                    check=True,
                    capture_output=True,
                    timeout=1800,
                )
                partial.replace(path)
            finally:
                partial.unlink(missing_ok=True)
            (run_dir / "runtime-distribution.json").write_bytes(
                built.report.read_bytes()
            )
            return path

        archive = resource_of(
            "Archive digest-pinned one-shot runtime images",
            save_images,
            lambda _inputs, _value: None,
            requires=(distribution,),
        )

        distributions: dict[str | None, Resource[RecipeDistribution]] = {
            None: distribution
        }
        archives: dict[str | None, Resource[Path]] = {None: archive}

    def images_key(node: NodeResource) -> str | None:
        return node.architecture if settings.runtime_distributions else None

    bootstraps = {
        key: _bootstrap(node, repo_root, run_dir=run_dir, settings=settings)
        for key, node in resources.nodes.items()
    }
    logs = settings.observability.logs
    operator_logging = None
    routes: dict[str, Resource[str]] = {}
    if logs.enabled and any(
        node.site_host is not None or node.request.lifecycle == "azure"
        for node in resources.nodes.values()
    ):
        operator_logging = environment.operator_logging
        if operator_logging is None:
            raise ValueError("a multi-site run needs the environment's logging")
        # Collectors push through their route to the platform's Loki, which
        # publishes on the operator's loopback `operator_logging.port`.
        routes = log_routes(
            resources,
            operator_logging,
            run_dir=run_dir,
            settings=settings,
            supervision=supervision,
        )
    platform = one_shot_platform(
        resources,
        settings,
        run_dir=run_dir,
        nanofaas_root=repo_root,
        # Behind a site's NAT the exporters are published with the node's
        # other ports; local and Azure nodes are scraped at their own address.
        forwards={
            key: published(node.site_host.site.address, node.ordinal).exporters()
            for key, node in resources.nodes.items()
            if node.site_host is not None
        },
        mode="BASELINE" if baseline else forecast_provider,
        operator_logging=operator_logging,
        log_endpoints=routes,
        bootstraps=bootstraps,
    )

    endpoints = {
        key: _control_plane(
            node,
            resources,
            bootstrap=bootstraps[key],
            platform=platform.resource,
            logs_enabled=logs.enabled,
            scientific=settings.purpose == "scientific-experiment",
            strict_memory=settings.strict_memory,
            neighbor_graph=graph if settings.neighbors is not None else None,
            distribution=distributions[images_key(node)],
            archive=archives[images_key(node)],
            architecture=node.architecture if settings.runtime_distributions else None,
            run_dir=run_dir,
            forecast_provider=forecast_provider,
            forecast_max_age_seconds=forecast_max_age_seconds,
            forecast_window_seconds=forecast_window_seconds,
            forecast_properties=forecast_properties,
            settings=settings,
            terminal_queueing=terminal_queueing,
            baseline_depth=max(
                fn.max_replicas for fn in resolve_node_functions(settings, key).values()
            )
            if baseline
            else None,
        )
        for key, node in resources.nodes.items()
    }
    probes = {key: node_probe_resource(node) for key, node in resources.nodes.items()}

    def pinned_for(key: str | None) -> Resource[RecipeDistribution]:
        members = [n for n in resources.nodes.values() if images_key(n) == key]

        def pin(inputs: TaskInputs) -> RecipeDistribution:
            built = inputs.resource(distributions[key])
            identities = archive_image_ids(
                inputs.resource(archives[key]),
                {
                    component.image.reference: component.image.id
                    for component in built.components
                },
            )
            components = []
            for component in built.components:
                actual_ids = {
                    node.command(
                        (
                            "docker",
                            "image",
                            "inspect",
                            "--format={{.Id}}",
                            component.image.reference,
                        )
                    ).stdout.strip()
                    for node in members
                }
                if len(actual_ids) != 1 or not actual_ids.issubset(
                    identities[component.image.reference]
                ):
                    raise ValueError("target image identity mismatch across nodes")
                actual = actual_ids.pop()
                components.append(
                    replace(
                        component,
                        image=replace(component.image, id=actual, reference=actual),
                    )
                )
            return replace(built, components=tuple(components))

        return resource_of(
            "Pin function registrations to immutable target image IDs"
            + (f" ({key})" if key else ""),
            pin,
            lambda _inputs, _value: None,
            requires=(
                distributions[key],
                archives[key],
                *(endpoints[n.config.id] for n in members),
            ),
        )

    pinned_by_key = {key: pinned_for(key) for key in distributions}
    functions = {
        definition.family: settings.functions[key]
        for key, definition in definitions.items()
    }
    if len(functions) != len(definitions):
        raise ValueError("one-shot function names must be unique")
    bindings = {
        key: RecipeBinding(
            distribution=pinned,
            functions={
                definition.family: (definition.family, definition.runtime)
                for definition in definitions.values()
            },
            run_dir=run_dir,
        )
        for key, pinned in pinned_by_key.items()
    }
    node_functions = {
        node.id: {
            definition.family: resolve_node_functions(settings, node.id)[key]
            for key, definition in definitions.items()
        }
        for node in settings.nodes
    }
    platforms = {}
    for key, node in resources.nodes.items():
        endpoint = endpoints[key]
        platform_functions = tuple(
            PlatformFunction(
                name=name,
                image="resolved-from-recipe",
                payload=json.dumps(function.input),
                build_argv=(),
                resources={
                    "limits": {"cpu": function.cpu, "memoryMiB": function.memory_mib},
                    **(
                        {
                            "requests": {
                                "cpu": function.cpu,
                                "memoryMiB": function.memory_mib,
                            }
                        }
                        if settings.strict_memory
                        else {}
                    ),
                },
                scaling_config={
                    "strategy": "NONE",
                    "minReplicas": 1,
                    "maxReplicas": function.max_replicas,
                    "concurrencyControl": {
                        "mode": "STATIC_PER_POD",
                        "targetInFlightPerPod": 1,
                    },
                },
                concurrency=function.max_replicas,
                queue_size=100,
                timeout_ms=30000,
                max_retries=max_retries,
                runtime_mode="HTTP",
                env={
                    "NANOFAAS_ONE_SHOT_PROFILE": "true",
                    "NANOFAAS_MAX_CONCURRENT_HANDLERS": "1",
                    **({"FUNCTION_HANDLER": name} if name in WORKLOAD_NAMES else {}),
                    **({"NANOFAAS_LOG_FORMAT": "json"} if logs.enabled else {}),
                },
            )
            for name, function in node_functions[key].items()
        )
        request = PlatformRequest(
            backend="container",
            functions=platform_functions,
            recipe=bindings[images_key(node)],
            label=key,
        )
        platforms[key] = add_platform(
            workflow,
            request,
            executor=NodeExecutor(node),
            requires=(endpoint,),
            local_endpoint="http://127.0.0.1:8080",
        )
    primary = images_key(next(iter(resources.nodes.values())))
    return OneShotTopology(
        resources,
        platforms,
        endpoints,
        probes,
        pinned_by_key[primary],
        functions,
        {key: definition.family for key, definition in definitions.items()},
        platform,
        None
        if supervision is None
        else workbench_supervision(
            platform, supervision, requires=tuple(endpoints.values())
        ),
        node_functions,
        graph,
        node_distributions={
            key: pinned_by_key[images_key(node)]
            for key, node in resources.nodes.items()
        }
        if settings.runtime_distributions
        else {},
    )
