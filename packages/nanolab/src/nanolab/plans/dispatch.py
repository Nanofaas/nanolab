"""Choose and build the plan a scenario's workflow names.

The CLI and the TUI run scenarios the same way: one builder per workflow,
with the bindings, run directories and remote roots that workflow needs.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from uuid import uuid4

from sonata_engine import Workflow, WorkflowObserver

from nanolab.application.execution import build_role_bindings
from nanolab.application.vm_provider import vm_request_for_role
from nanolab.comparison.profiles import PreparedComparison
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.plans.cli import build_cli_plan
from nanolab.plans.loadtest import build_loadtest_plan
from nanolab.plans.offload import build_offload_plan
from nanolab.plans.offload_loadtest import build_offload_loadtest_plan
from nanolab.plans.runtime_comparison import (
    build_runtime_comparison_plan,
    is_runtime_comparison,
)
from nanolab.plans.soak import unique_soak_run_dir
from nanolab.plans.validate import (
    build_validate_plan,
    recipe_run_tag,
    require_recipe_environment,
)
from nanolab.tasks.loadtest.adapters import HttpPrometheusClient
from nanolab.tasks.recipes.remote import remote_recipe_root
from nanolab.tasks.telegram import telegram_observer_from_environment
from nanolab.workspace.paths import ToolPaths, default_tool_paths

LOCAL_PROMETHEUS_URL = "http://127.0.0.1:9090"


def scenario_observers(scenario_path: Path) -> tuple[WorkflowObserver, ...]:
    """Return the observers a run of this scenario reports to, if any."""
    observer = telegram_observer_from_environment(scenario_path.name)
    return (observer,) if observer is not None else ()


def build_scenario_workflow(  # NOSONAR (S3776): one branch per workflow
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    paths: ToolPaths | None = None,
    control_plane_url: str | None = None,
    prometheus_url: str = LOCAL_PROMETHEUS_URL,
    run_dir: Path | None = None,
    dry_run: bool = False,
    prepared_comparison: PreparedComparison | None = None,
    scenario_path: Path | None = None,
) -> Workflow:
    """Return the workflow that runs `scenario` on `environment`.

    `paths` defaults to the operator's tool paths. Without `run_dir`, each
    workflow picks its own default under them.
    """
    paths = paths or default_tool_paths()
    if scenario.workflow.startswith("one-shot-"):
        from nanolab.plans.one_shot_calibration import build_one_shot_calibration_plan
        from nanolab.plans.one_shot_experiment import build_one_shot_experiment_plan
        from nanolab.plans.one_shot_qualification import (
            build_one_shot_qualification_plan,
        )

        builders = {
            "one-shot-calibration": build_one_shot_calibration_plan,
            "one-shot-qualification": build_one_shot_qualification_plan,
            "one-shot-experiment": build_one_shot_experiment_plan,
        }
        return builders[scenario.workflow](
            scenario,
            environment,
            run_dir=run_dir or paths.runs_dir / f"one-shot-{uuid4().hex}",
            repo_root=paths.nanofaas_root,
        )
    if (
        scenario.workflow in {"validate", "loadtest"}
        and scenario.recipe_profile is not None
        and run_dir is None
    ):
        run_dir = paths.runs_dir / (
            "recipe-preview" if dry_run else f"recipe-{uuid4().hex}"
        )
    if prepared_comparison is not None and (
        scenario.workflow != "loadtest" or not is_runtime_comparison(scenario)
    ):
        raise ValueError("prepared comparison can only be used by runtime comparison")
    remote_project_root = (
        str(prepared_comparison.remote_source)
        if prepared_comparison is not None
        else None
    )
    if (
        scenario.workflow == "validate"
        and scenario.backend in {"k8s", "containerd"}
        and scenario.recipe_profile is not None
    ):
        require_recipe_environment(scenario, environment)
        if environment.provider == "multipass":
            if run_dir is None:
                raise ValueError("recipe validation requires a run directory")
            remote_project_root = str(
                remote_recipe_root(
                    vm_request_for_role(environment, "stack"), recipe_run_tag(run_dir)
                )
                / "source"
            )
    if remote_project_root is None:
        bindings, fetcher = build_role_bindings(environment)
    else:
        bindings, fetcher = build_role_bindings(
            environment,
            repo_root=paths.nanofaas_root,
            remote_project_root=remote_project_root,
        )
    if scenario.workflow == "contract":
        from nanolab.plans.contract import build_contract_plan

        if scenario_path is None:
            raise ValueError("contract requires the resolved scenario path")
        return build_contract_plan(
            scenario,
            bindings,
            repo_root=paths.nanofaas_root,
            environment=environment,
            run_dir=run_dir or paths.runs_dir / "contract-preview",
            scenario_path=scenario_path,
        )
    if scenario.workflow == "soak":
        from nanolab.plans.soak import build_soak_plan

        if scenario.backend == "container" and environment.provider != "local":
            raise ValueError("soak currently requires a local container environment")
        return build_soak_plan(
            scenario,
            environment,
            bindings,
            run_dir=run_dir or unique_soak_run_dir(paths.runs_dir),
            repo_root=paths.nanofaas_root,
            tool_root=paths.tool_root,
        )
    if scenario.workflow == "heap-analysis":
        from nanolab.plans.heap_analysis import (
            build_heap_analysis_plan,
            unique_heap_analysis_run_dir,
        )

        if environment.provider != "local":
            raise ValueError(
                "heap analysis currently requires a local container environment"
            )
        return build_heap_analysis_plan(
            scenario,
            environment,
            bindings,
            run_dir=run_dir or unique_heap_analysis_run_dir(paths.runs_dir),
            repo_root=paths.nanofaas_root,
            tool_root=paths.tool_root,
        )
    if scenario.workflow == "idle-reactivation":
        from nanolab.plans.idle_reactivation import build_idle_reactivation_plan

        return build_idle_reactivation_plan(
            scenario,
            environment,
            bindings,
            control_plane_url=control_plane_url or "http://127.0.0.1:30080",
            prometheus_client=HttpPrometheusClient(
                prometheus_url, preserve_labels=True
            ),
            run_dir=run_dir or paths.runs_dir / f"idle-reactivation-{uuid4().hex}",
            repo_root=paths.nanofaas_root,
            tool_root=paths.tool_root,
            fetcher=fetcher,
        )
    if scenario.workflow == "validate":
        return build_validate_plan(
            scenario,
            bindings,
            repo_root=paths.nanofaas_root,
            tool_root=paths.tool_root,
            environment=environment,
            run_dir=run_dir or paths.runs_dir / "recipe-preview",
        )
    if scenario.workflow == "offload":
        return build_offload_plan(scenario, bindings, repo_root=paths.nanofaas_root)
    if scenario.workflow == "offload-loadtest":
        return build_offload_loadtest_plan(
            scenario,
            environment,
            bindings,
            run_dir=run_dir or paths.runs_dir / "latest",
            repo_root=paths.nanofaas_root,
            tool_root=paths.tool_root,
            fetcher=fetcher,
            dry_run=dry_run,
        )
    if scenario.workflow == "cli":
        return build_cli_plan(
            scenario,
            bindings,
            endpoint=control_plane_url,
            repo_root=paths.nanofaas_root,
            environment=environment,
            run_dir=run_dir,
        )
    # The comparison is a load test in every respect the platform cares about, so
    # it shares the workflow name and the provisioning that goes with it, and only
    # the plan builder differs.
    builder = (
        build_runtime_comparison_plan
        if is_runtime_comparison(scenario)
        else build_loadtest_plan
    )
    if prepared_comparison is not None:
        builder = partial(build_runtime_comparison_plan, prepared=prepared_comparison)
    return builder(
        scenario,
        environment,
        bindings,
        control_plane_url=control_plane_url or "http://127.0.0.1:8080",
        prometheus_client=HttpPrometheusClient(prometheus_url),
        run_dir=run_dir or paths.runs_dir / "latest",
        fetcher=fetcher,
        repo_root=paths.nanofaas_root,
        tool_root=paths.tool_root,
    )
