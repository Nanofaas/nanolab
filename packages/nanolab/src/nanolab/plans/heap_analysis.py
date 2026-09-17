"""Deferred plan for the control-plane heap-analysis workflow.

Compiles the ordered dump-around-workload-then-MAT lifecycle without touching
Docker, building anything, or making a network call: `RunControlPlaneHeapAnalysis`
performs every side effect, deferred behind the Sonata task it becomes.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sonata_engine import Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor
from sonata_tasks.registry import docker_registry_resource

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.deployment import REGISTRY_CONTAINER_NAME


def unique_heap_analysis_run_dir(runs_dir: Path) -> Path:
    """Return a fresh run directory that no earlier run can collide with."""
    return runs_dir / ("heap-analysis-" + uuid4().hex)


def build_heap_analysis_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    run_dir: Path,
    repo_root: Path,
    tool_root: Path,
) -> Workflow:
    """Compile the deferred heap-analysis lifecycle without any side effects."""
    if config.workflow != "heap-analysis" or config.heap_analysis is None:
        raise ValueError("build_heap_analysis_plan requires a heap-analysis scenario")
    if environment.provider != "local":
        raise ValueError("heap analysis currently requires a local environment")
    from nanolab.tasks.heap_analysis.runtime import RunControlPlaneHeapAnalysis

    workflow = Workflow(workflow_id="heap-analysis")
    # Acquired before the task runs, because preparation pushes the application
    # images and the helper build pushes the helper -- both into the registry
    # this run owns. It removes only what it created and leaves a registry it
    # found running alone, so an operator's own is never torn down by a run.
    registry = docker_registry_resource(
        executor=RoleBoundCommandTaskExecutor(bindings),
        role="host",
        container=REGISTRY_CONTAINER_NAME,
    )
    workflow.add(
        RunControlPlaneHeapAnalysis(
            config,
            bindings,
            run_dir=run_dir,
            repo_root=repo_root,
            tool_root=tool_root,
        ),
        requires=(registry,),
    )
    return workflow
