"""Compile B6 as its own workflow, sharing NanoLab's platform resources."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from sonata_engine import Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor

from nanolab.application.functions import resolve_function, sonata_function
from nanolab.application.vm_provider import vm_request_for_role
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.tasks.components.bootstrap import remote_project_dir
from nanolab.tasks.components.helm import control_plane_helm_values, helm_set_args
from nanolab.tasks.idle_reactivation_runtime import (
    RecordIdleInputs,
    RunIdleReactivation,
    VerifyIdlePair,
)
from nanolab.tasks.loadtest.ports import PrometheusClient, RemoteFileFetcher
from nanolab.tasks.platform import PlatformRequest, add_platform
from nanolab.workspace.provenance import source_fingerprint


def require_idle_environment(environment: EnvironmentConfig) -> None:
    """Reject an unmatched execution boundary before any VM is acquired."""
    if environment.provider == "local" or "loadgen" not in environment.roles:
        raise ValueError(
            "idle-reactivation requires remote stack and dedicated loadgen roles"
        )

    stack, loadgen = environment.target("stack"), environment.target("loadgen")
    if (stack.host and stack.host == loadgen.host) or (
        stack.name and stack.name == loadgen.name
    ):
        raise ValueError(
            "idle-reactivation requires distinct stack and loadgen machines"
        )


def build_idle_reactivation_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    control_plane_url: str,
    prometheus_client: PrometheusClient,
    run_dir: Path,
    repo_root: Path,
    tool_root: Path | None = None,
    fetcher: RemoteFileFetcher | None = None,
) -> Workflow:
    """Build once and measure two fresh deployments with compensated cleanup."""
    if config.workflow != "idle-reactivation" or config.idle_reactivation is None:
        raise ValueError("idle-reactivation requires its dedicated protocol")
    require_idle_environment(environment)
    if any((run_dir / arm).exists() for arm in config.idle_reactivation.treatments):
        raise ValueError(
            "B6 needs a fresh run directory; partial runs cannot be resumed"
        )
    root = Path(remote_project_dir(vm_request_for_role(environment, "stack")))
    function = sonata_function(
        resolve_function(
            config, config.functions[0], source_root=repo_root, tool_root=tool_root
        )
    )
    for name in ("performance-medium.json", "runtime-contract.json"):
        if not (repo_root / "functions/test-data/word-stats" / name).is_file():
            raise ValueError(f"B6 requires the B5 corpus and oracle: {name}")
    executor = RoleBoundCommandTaskExecutor(bindings)
    workflow = Workflow("Idle footprint and reactivation")
    workflow.add(RecordIdleInputs(config, repo_root, run_dir))
    fingerprint = source_fingerprint(repo_root)
    for index, treatment in enumerate(config.idle_reactivation.treatments):
        measured = replace(
            function,
            concurrency=2,
            queue_size=20,
            max_retries=3,
            timeout_ms=30000,
            scaling_config={
                "strategy": "INTERNAL",
                "minReplicas": int(treatment == "keep-ready"),
                "maxReplicas": 1,
                "metrics": [{"type": "rps", "target": "100"}],
            },
        )
        request = PlatformRequest(
            backend="k8s",
            functions=(measured,),
            additional_modules=("autoscaler", "async-queue", "build-metadata"),
            source_fingerprint=fingerprint,
            helm_chart=str(root / "deploy/helm/nanofaas"),
            label=treatment,
        )
        values = control_plane_helm_values(
            namespace=request.namespace,
            control_plane_image=request.control_plane_image_reference(),
            image_pull_policy="IfNotPresent",
            expose_node_port=True,
            metrics_profile="advanced",
            container_metrics=True,
            control_plane_resources=config.resources["control-plane"].model_dump(
                by_alias=True, exclude_none=True
            ),
        )
        env_index = sum(
            key.endswith(".name") and key.startswith("controlPlane.extraEnv[")
            for key in values
        )
        values[f"controlPlane.extraEnv[{env_index}].name"] = (
            "NANOFAAS_K8S_IMAGEPULLPOLICY"
        )
        values[f"controlPlane.extraEnv[{env_index}].value"] = "IfNotPresent"
        request = replace(request, helm_values=helm_set_args(values))
        if index:
            request = replace(
                request,
                build_images=False,
                build_control_plane=False,
                control_plane_image=request.control_plane_image_reference(),
            )
        platform = add_platform(workflow, request, executor=executor, cwd=repo_root)
        workflow.add(
            RunIdleReactivation(
                function=measured.name,
                treatment=treatment,
                namespace=request.namespace,
                protocol=config.idle_reactivation,
                executor=executor,
                prometheus_client=prometheus_client,
                fetcher=fetcher,
                run_dir=run_dir / treatment,
                remote_home=Path(
                    remote_project_dir(vm_request_for_role(environment, "loadgen"))
                ).parent,
                control_plane_url=control_plane_url,
            ),
            requires=(*platform.resources, *platform.functions),
        )
    # No deployment dependencies: Sonata releases the last arm before this gate.
    workflow.add(VerifyIdlePair(run_dir))
    return workflow
