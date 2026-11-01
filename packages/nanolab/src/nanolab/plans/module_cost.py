"""A matched module block composed from normal NanoLab platform resources."""

from pathlib import Path

from sonata_engine import Resource, Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor

from nanolab.application.vm_provider import vm_request_for_role
from nanolab.assets.diagnostics.idle_reactivation_probe import load_inputs
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.tasks.components.bootstrap import remote_project_dir
from nanolab.tasks.loadtest.ports import PrometheusClient, RemoteFileFetcher
from nanolab.tasks.module_cost import AnalyzeModuleBlock, RunModuleArm
from nanolab.tasks.module_cost_build import BuildModuleCost


def require_module_environment(environment: EnvironmentConfig) -> None:
    """Validate ownership before provider acquisition, also for direct plan callers."""
    if (
        environment.provider != "azure"
        or not environment.azure
        or environment.azure.priority != "Spot"
    ):
        raise ValueError("module-cost requires workflow-created Azure Spot machines")
    if set(environment.roles) != {"stack", "loadgen"}:
        raise ValueError("module-cost requires distinct stack and loadgen roles")
    targets = [environment.target(role) for role in ("stack", "loadgen")]
    if any(t.host or not t.name for t in targets):
        raise ValueError("module-cost machines must be created by the workflow")
    if targets[0].name == targets[1].name:
        raise ValueError("module-cost requires distinct machines")


def build_module_cost_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    run_dir: Path,
    repo_root: Path,
    control_plane_url: str,
    prometheus_client: PrometheusClient,
    fetcher: RemoteFileFetcher | None = None,
) -> Workflow:
    """Compile without allocation or writes; all recipes finish before any arm."""
    if config.workflow != "module-cost" or config.module_cost is None:
        raise ValueError("module-cost requires its protocol")
    require_module_environment(environment)
    if any(
        (run_dir / name).exists()
        for name in ("protocol.json", "inputs", "build", "arms")
    ):
        raise ValueError(
            "module-cost requires fresh results; never resume a partial block"
        )
    corpus = repo_root / "functions/test-data/word-stats"
    load_inputs(corpus / "performance-medium.json", corpus / "runtime-contract.json")
    executor = RoleBoundCommandTaskExecutor(bindings)
    source = Path(remote_project_dir(vm_request_for_role(environment, "stack")))
    load_home = Path(
        remote_project_dir(vm_request_for_role(environment, "loadgen"))
    ).parent
    remote = source.parent / "module-cost" / config.module_cost.block_id
    build = BuildModuleCost(
        config.module_cost, repo_root, run_dir, remote, source, executor
    )
    images = Resource(
        title="Assemble all module-cost images",
        acquire=lambda inputs: build.run(inputs).value,
        release=lambda _inputs, _value: None,
    )
    workflow = Workflow("Matched module cost")
    for arm in config.module_cost.order:
        workflow.add(
            RunModuleArm(
                arm,
                images,
                executor,
                run_dir,
                repo_root,
                source,
                load_home,
                control_plane_url,
                prometheus_client,
                fetcher,
            ),
            requires=(images,),
        )
    workflow.add(AnalyzeModuleBlock(run_dir))
    return workflow
