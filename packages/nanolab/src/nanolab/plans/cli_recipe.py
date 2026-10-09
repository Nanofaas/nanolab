"""Local recipe-backed CLI qualification; builds remain independent of Kubernetes."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import yaml
from sonata_engine import Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor
from sonata_tasks.kubectl import PinnedKubeconfigExecutor

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.cli_artifacts import (
    CliArtifactCheckTask,
    cli_artifact_resource,
    cli_modes,
)
from nanolab.tasks.components.helm import control_plane_helm_values, helm_set_args
from nanolab.tasks.platform import (
    PlatformFunction,
    PlatformRequest,
    _resolve_platform_endpoint,
)
from nanolab.tasks.recipes.kubernetes import (
    minikube_images_resource,
    minikube_target_resource,
    recipe_namespace_resource,
)
from nanolab.tasks.recipes.workflow import (
    RecipeBinding,
    assembled_recipe_distribution_resource,
    recipe_run_resource,
)
from nanolab.tasks.validation.cli_parity import CliParityTask

_MODULES = frozenset({"k8s-deployment-provider", "build-metadata", "runtime-config"})


def build_recipe_cli_plan(
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    repo_root: Path,
    environment: EnvironmentConfig | None,
    run_dir: Path,
) -> Workflow:
    """Compile an owned attempt without freezing, building or provisioning anything."""
    modes = cli_modes(config.cli_runtime)
    if (
        config.workflow != "cli"
        or config.backend != "k8s"
        or config.recipe_profile is None
        or (environment is not None and environment.provider != "local")
    ):
        raise ValueError("recipe CLI requires a local k8s cli scenario")
    if config.functions != ["word-stats-java"]:
        raise ValueError("recipe CLI requires only Java JVM word-stats")
    if (
        config.build != "docker"
        or config.control_plane_runtime != "jvm"
        or config.control_plane_image is not None
        or config.control_plane_variant is not None
        or config.function_images
        or config.async_load
        or config.handler_envelope
        or config.persistent_recovery
        or config.retry_backoff_burst
    ):
        raise ValueError("recipe CLI scenario contains incompatible options")
    profile = yaml.safe_load(config.recipe_profile.read_text())
    if not isinstance(profile, dict) or not isinstance(
        profile.get("controlPlane"), dict
    ):
        raise ValueError("recipe CLI profile lacks a control plane")
    control = profile["controlPlane"]
    if (
        control.get("build", {}).get("mode") != "jvm"
        or set(control.get("modules", [])) != _MODULES
    ):
        raise ValueError(
            "recipe CLI requires the JVM control plane and exact CLI modules"
        )
    functions = profile.get("functions", [])
    if (
        len(functions) != 1
        or functions[0].get("name") != "word-stats"
        or functions[0].get("sdk") != "java"
        or functions[0].get("build", {}).get("mode") != "jvm"
        or profile.get("services")
    ):
        raise ValueError("recipe CLI requires exactly one Java JVM word-stats image")
    token = uuid4().hex[:12]
    attempt = run_dir / "cli-attempts" / token
    namespace = f"nanofaas-cli-{token}"
    tag = f"cli-{token}"
    host = RoleBoundCommandTaskExecutor(bindings)
    kubeconfig = attempt / "selected-kubeconfig.json"
    target = minikube_target_resource(executor=host, kubeconfig=kubeconfig)
    pinned = RoleBoundCommandTaskExecutor(
        RoleBindings(
            {
                role: PinnedKubeconfigExecutor(bindings.executor_for(role), kubeconfig)
                for role in ("host", "stack")
            }
        )
    )
    staged = recipe_run_resource(
        source=repo_root,
        recipe=config.recipe_profile,
        run_dir=attempt / "recipe",
        tag=tag,
    )
    artifacts = tuple(
        cli_artifact_resource(
            staged, mode=mode, executor=host, evidence_dir=attempt / "artifacts"
        )
        for mode in modes
    )
    distribution = assembled_recipe_distribution_resource(
        run=staged,
        executor=host,
        functions=(("word-stats", "java"),),
        required_modules=_MODULES,
    )
    images = minikube_images_resource(
        target=target, distribution=distribution, executor=pinned, run_dir=attempt
    )
    owned_namespace = recipe_namespace_resource(
        namespace=namespace,
        executor=pinned,
        role="host",
        target=target,
        requires=(images,),
    )
    binding = RecipeBinding(
        distribution,
        {"word-stats-java": ("word-stats", "java")},
        attempt,
        target=target,
    )
    values = control_plane_helm_values(
        namespace=namespace,
        control_plane_image="127.0.0.1:5000/nanofaas/control-plane:deferred",
        image_pull_policy="IfNotPresent",
        admin_runtime_config=True,
        control_plane_resources=config.resources["control-plane"].model_dump(
            by_alias=True, exclude_none=True
        )
        if "control-plane" in config.resources
        else None,
    )
    request = PlatformRequest(
        backend="k8s",
        functions=(PlatformFunction("word-stats-java", "deferred", "{}", ("true",)),),
        namespace=namespace,
        recipe=binding,
        helm_release=f"cli-{token}",
        helm_chart=str((attempt / "recipe/source/deploy/helm/nanofaas").resolve()),
        helm_values=helm_set_args(values),
        execution_role="host",
    )
    resources, endpoint = _resolve_platform_endpoint(
        request, None, pinned, None, (owned_namespace,), ""
    )
    workflow = Workflow(workflow_id="cli")
    for artifact in artifacts:
        workflow.add(
            CliArtifactCheckTask(staged, artifact), requires=(staged, artifact)
        )
    workflow.add(
        CliParityTask(
            staged,
            distribution,
            artifacts,
            runtime=config.cli_runtime,
            endpoint=endpoint,
            target=target,
            namespace=namespace,
            function="word-stats-java",
            executor=pinned,
            evidence_dir=attempt / "qualification",
            function_resources=config.resources["word-stats-java"].model_dump(
                by_alias=True, exclude_none=True
            )
            if "word-stats-java" in config.resources
            else None,
        ),
        requires=(
            staged,
            *artifacts,
            distribution,
            target,
            images,
            owned_namespace,
            *resources,
        ),
    )
    return workflow
