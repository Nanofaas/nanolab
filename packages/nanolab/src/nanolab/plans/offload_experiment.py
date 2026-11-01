"""B4 reuses the two-platform workflow with one matched, measurable function."""

from dataclasses import replace
from pathlib import Path

from sonata_engine import Steps, Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor

from nanolab.application.functions import resolve_function, sonata_function
from nanolab.application.vm_provider import vm_request_for_role
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.environment import require_experiment_environment
from nanolab.plans.diagnostics import collect_control_plane_log
from nanolab.plans.offload_loadtest import _platform, _role_host
from nanolab.tasks.components.bootstrap import remote_project_dir
from nanolab.tasks.components.helm import control_plane_helm_values, helm_set_args
from nanolab.tasks.loadtest.ports import RemoteFileFetcher
from nanolab.tasks.offload_experiment import RunOffloadExperiment
from nanolab.tasks.offload_loadtest import (
    OffloadLoadtestRequest,
    build_offload_loadtest_workflow,
)


def build_experiment(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    run_dir: Path,
    repo_root: Path,
    fetcher: RemoteFileFetcher | None,
    dry_run: bool = False,
) -> Workflow:
    """Compose matched platforms and evidence collection using the existing workflow."""
    require_experiment_environment(environment)
    if any(
        (run_dir / name).exists()
        for name in ("protocol.json", "inputs", "observations", "k6-events.jsonl")
    ):
        raise ValueError("B4 requires a fresh output directory")
    if fetcher is None:
        raise ValueError("B4 requires the generator's remote file fetcher")
    protocol = config.offload_experiment
    if protocol is None:
        raise ValueError("offloadExperiment is required")
    for name in ("performance-medium.json", "runtime-contract.json"):
        if not (repo_root / "functions/test-data/word-stats" / name).is_file():
            raise ValueError(
                f"B4 requires the archived medium corpus and oracle: {name}"
            )
    edge_url = f"http://{_role_host(environment, 'stack', dry_run=dry_run)}:30080"
    cloud_url = f"http://{_role_host(environment, 'cloud', dry_run=dry_run)}:30080"
    function = replace(
        sonata_function(
            resolve_function(config, config.functions[0], source_root=repo_root)
        ),
        concurrency=2,
        queue_size=20,
        max_retries=0,
        timeout_ms=30000,
        offload={"enabled": protocol.mode == "pressure", "mode": "pressure"},
    )
    platforms = {}
    for role in ("stack", "cloud"):
        current = (
            function
            if role == "stack"
            else replace(
                function, concurrency=20, queue_size=100, offload={"enabled": False}
            )
        )
        request = _platform(
            (current,),
            label="edge" if role == "stack" else "cloud",
            role=role,
            build=config.build,
            repo_root=repo_root,
            backend="k8s",
        )
        values = control_plane_helm_values(
            namespace=request.namespace,
            control_plane_image=request.control_plane_image_reference(),
            expose_node_port=True,
            metrics_profile="advanced",
            sync_queue_admission_enabled=True,
            sync_queue_max_depth=20 if role == "stack" else 100,
            container_metrics=True,
            control_plane_resources=config.resources["control-plane"].model_dump(
                by_alias=True, exclude_none=True
            ),
        )
        extra = [("NANOFAAS_ADMISSION_PROFILE", "SYNC_QUEUE")]
        if role == "stack":
            extra.append(("NANOFAAS_OFFLOAD_TARGETURL", cloud_url))
        index = sum(k.endswith(".name") for k in values)
        for name, value in extra:
            values[f"controlPlane.extraEnv[{index}].name"] = name
            values[f"controlPlane.extraEnv[{index}].value"] = value
            index += 1
        platforms[role] = replace(
            request,
            helm_values=helm_set_args(values),
            helm_chart=str(
                Path(remote_project_dir(vm_request_for_role(environment, role)))
                / "deploy/helm/nanofaas"
            ),
        )
    executor = RoleBoundCommandTaskExecutor(bindings)
    task = RunOffloadExperiment(
        config=config,
        source_root=repo_root,
        run_dir=run_dir,
        remote_home=Path(
            remote_project_dir(vm_request_for_role(environment, "loadgen"))
        ).parent,
        edge_url=edge_url,
        cloud_url=cloud_url,
        executor=executor,
        fetcher=fetcher,
        namespace=platforms["stack"].namespace,
        logs=tuple(
            collect_control_plane_log(
                executor=executor,
                role=role,
                namespace=platforms[role].namespace,
                destination=run_dir / f"control-plane-{role}.log",
            )
            for role in ("stack", "cloud")
        ),
    )
    return build_offload_loadtest_workflow(
        OffloadLoadtestRequest(cloud=platforms["cloud"], edge=platforms["stack"]),
        bindings,
        cwd=repo_root,
        load=Steps(title="Run B4 offload experiment", steps=(task,)),
        edge_local_endpoint=edge_url,
        cloud_local_endpoint=cloud_url,
    )
