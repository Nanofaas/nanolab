"""Shared container deployment composition for the three independent workflows."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx
from sonata_engine import Resource, TaskInputs, Workflow
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from nanolab.application.execution import build_role_bindings
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.one_shot import OneShotFunction
from nanolab.functions.catalog import resolve_function_definition
from nanolab.one_shot.infrastructure import (
    NodeExecutor,
    NodeResource,
    OneShotResources,
    build_one_shot_resources,
)
from nanolab.tasks.components.bootstrap import plan_vm_provision_base
from nanolab.tasks.one_shot.images import archive_image_ids
from nanolab.tasks.one_shot.resources import node_probe_resource
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

    @property
    def requires(self) -> tuple[Resource[Any], ...]:
        """Keep every node, platform and probe alive through the consumer."""
        return (
            *self.resources.vms,
            self.resources.generator,
            *self.endpoints.values(),
            *self.probes.values(),
            self.distribution,
            *(
                function
                for platform in self.platforms.values()
                for function in platform.functions
            ),
        )


def _bootstrap(node: NodeResource, root: Path) -> Resource[None]:
    def acquire(inputs: TaskInputs) -> None:
        vm = inputs.resource(node.vm)
        request = node.request.model_copy(
            update={"host": vm.host, "user": vm.user, "home": vm.home}
        )
        assets = Path(str(files("nanolab").joinpath("assets")))
        context = scenario_context(root, request, assets)
        operations = retarget_cloud_operations(
            node.provider, context, plan_vm_provision_base(context)
        )
        run_bootstrap_operations(node.provider, operations, role=node.config.id)

    return Resource(
        title=f"Bootstrap {node.config.id} container VM",
        acquire=acquire,
        release=lambda _inputs, _value: None,
        requires=(node.vm,),
    )


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
        built = inputs.resource(distribution)
        image_archive = inputs.resource(archive)
        remote = f"{vm.home}/one-shot-images.tar"
        transferred = node.provider.transfer_to(
            node.request, source=image_archive, destination=remote
        )
        if transferred.return_code != 0:
            raise RuntimeError("one-shot image transfer failed")
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
            loaded_ids[built.control_plane().image.reference],
            "--server.port=8080",
            "--management.server.port=9090",
            "--logging.level.root=WARN",
            "--nanofaas.admission.profile=SYNC_QUEUE",
            "--nanofaas.metrics.profile=advanced",
            "--nanofaas.registry.path=/tmp/functions.json",
            "--nanofaas.container-local.runtime-adapter=docker-java",
            "--nanofaas.container-local.bind-host=0.0.0.0",
            f"--nanofaas.container-local.namespace={vm.name}",
            f"--nanofaas.p2p.enabled={boolean}",
            f"--nanofaas.p2p.admin.enabled={boolean}",
            f"--nanofaas.p2p.node-id={node.config.id}",
            "--nanofaas.p2p.port=14200",
            f"--nanofaas.p2p.external-host={vm.host}",
            f"--nanofaas.p2p.invocation-uri=http://{vm.host}:8080",
            "--nanofaas.p2p.ping-interval=250ms",
            "--nanofaas.p2p.ping-timeout=1s",
            f"--nanofaas.forecasting.enabled={boolean}",
            f"--nanofaas.forecasting.node-id={node.config.id}",
            f"--nanofaas.forecasting.provider={forecast_provider}",
            "--nanofaas.forecasting.window=1s",
            f"--nanofaas.offload.one-shot.enabled={boolean}",
            "--sync-queue.enabled="
            + str(
                (not edge and terminal_queueing)
                or (edge and baseline_depth is not None)
            ).lower(),
        ]
        if edge and baseline_depth is not None:
            cloud = next(
                other
                for other in topology.nodes.values()
                if other.config.kind == "cloud"
            )
            cloud_vm = inputs.resource(cloud.vm)
            args.extend(
                (
                    f"--nanofaas.offload.target-url=http://{cloud_vm.host}:8080",
                    f"--sync-queue.max-depth={baseline_depth}",
                )
            )
        if edge:
            peers = [
                inputs.resource(other.vm).host
                for other in topology.nodes.values()
                if other.config.kind == "edge" and other is not node
            ]
            args.extend(
                f"--nanofaas.p2p.seeds[{index}]={host}:14200"
                for index, host in enumerate(peers)
            )
        node.command(tuple(args))
        deadline = time.monotonic() + 90
        with httpx.Client(trust_env=False) as http:
            while time.monotonic() < deadline:
                try:
                    http.get(
                        f"http://{vm.host}:9090/actuator/health/readiness", timeout=2
                    ).raise_for_status()
                    http.get(
                        f"http://{vm.host}:8080/v1/functions", timeout=2
                    ).raise_for_status()
                    return f"http://{vm.host}:8080"
                except httpx.HTTPError:
                    time.sleep(0.2)
        raise RuntimeError(f"{node.config.id} control-plane readiness timed out")

    return compensated_resource(
        title=f"Acquire {node.config.id} control plane",
        acquire=acquire,
        release=release,
        compensate=release,
        requires=(bootstrap, distribution, archive, *topology.vms),
    )


def add_one_shot_platforms(
    workflow: Workflow,
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    run_dir: Path,
    repo_root: Path,
    forecast_provider: str = "ORACLE",
    terminal_queueing: bool = False,
    baseline: bool = False,
) -> OneShotTopology:
    """Compose existing staged recipes, bootstrap and function resources per VM."""
    settings = config.one_shot
    if settings is None:
        raise ValueError("one-shot configuration missing")
    run_dir.mkdir(parents=True, exist_ok=True)
    resources = build_one_shot_resources(settings, environment, repo_root=repo_root)
    definitions = {
        key: resolve_function_definition(key, repo_root) for key in config.functions
    }
    selected = tuple(
        (definition.family, definition.runtime) for definition in definitions.values()
    )
    recipe = config.recipe_profile or Path(
        str(
            files("nanolab").joinpath(
                "assets", "presets", "recipes", "one-shot-jvm.yaml"
            )
        )
    )
    if config.control_plane_runtime != "jvm" and config.recipe_profile is None:
        raise ValueError("native one-shot requires an explicit native recipeProfile")
    executor = RoleBoundCommandTaskExecutor(
        build_role_bindings(EnvironmentConfig(provider="local"))[0]
    )
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
            settings.runtime_distribution.path, recipe=recipe, tag=tag, published=False
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
        distribution = Resource(
            title="Reuse immutable one-shot runtime distribution",
            acquire=lambda _inputs: checked,
            release=lambda _inputs, _value: None,
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
                )
                .stdout.decode()
                .strip()
            )
            if observed != component.image.id:
                raise ValueError("local image changed since runtime build")
        subprocess.run(
            (
                "docker",
                "save",
                "--output",
                str(path),
                *(component.image.reference for component in built.components),
            ),
            check=True,
            capture_output=True,
        )
        (run_dir / "runtime-distribution.json").write_bytes(built.report.read_bytes())
        return path

    archive = Resource(
        title="Archive digest-pinned one-shot runtime images",
        acquire=save_images,
        release=lambda _inputs, _value: None,
        requires=(distribution,),
    )

    endpoints = {
        key: _control_plane(
            node,
            resources,
            bootstrap=_bootstrap(node, repo_root),
            distribution=distribution,
            archive=archive,
            run_dir=run_dir,
            forecast_provider=forecast_provider,
            terminal_queueing=terminal_queueing,
            baseline_depth=max(fn.max_replicas for fn in settings.functions.values())
            if baseline
            else None,
        )
        for key, node in resources.nodes.items()
    }
    probes = {key: node_probe_resource(node) for key, node in resources.nodes.items()}

    def pin(inputs: TaskInputs) -> RecipeDistribution:
        built = inputs.resource(distribution)
        identities = archive_image_ids(
            inputs.resource(archive),
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
                for node in resources.nodes.values()
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

    pinned = Resource(
        title="Pin function registrations to immutable target image IDs",
        acquire=pin,
        release=lambda _inputs, _value: None,
        requires=(distribution, archive, *endpoints.values()),
    )
    functions = {
        definition.family: settings.functions[key]
        for key, definition in definitions.items()
    }
    if len(functions) != len(definitions):
        raise ValueError("one-shot function names must be unique")
    binding = RecipeBinding(
        distribution=pinned,
        functions={
            definition.family: (definition.family, definition.runtime)
            for definition in definitions.values()
        },
        run_dir=run_dir,
    )
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
                    "limits": {"cpu": function.cpu, "memoryMiB": function.memory_mib}
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
                max_retries=0,
                runtime_mode="HTTP",
                env={
                    "NANOFAAS_ONE_SHOT_PROFILE": "true",
                    "NANOFAAS_MAX_CONCURRENT_HANDLERS": "1",
                },
            )
            for name, function in functions.items()
        )
        request = PlatformRequest(
            backend="container", functions=platform_functions, recipe=binding, label=key
        )
        platforms[key] = add_platform(
            workflow,
            request,
            executor=NodeExecutor(node),
            requires=(endpoint,),
            local_endpoint="http://127.0.0.1:8080",
        )
    return OneShotTopology(
        resources,
        platforms,
        endpoints,
        probes,
        pinned,
        functions,
        {key: definition.family for key, definition in definitions.items()},
    )
