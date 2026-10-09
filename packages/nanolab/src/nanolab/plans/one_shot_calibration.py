"""The independently executable physical service-calibration workflow."""

from pathlib import Path

from sonata_engine import Steps, Workflow

from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.plans.one_shot_common import add_one_shot_platforms
from nanolab.tasks.one_shot.calibration import (
    MeasureServiceTask,
    PublishCalibrationTask,
    ValidateCapacityTask,
    WarmupTask,
)
from nanolab.tasks.one_shot.clock import clock_health_resource
from nanolab.tasks.one_shot.preflight import CollectTopologyTask
from nanolab.workspace.paths import default_tool_paths


def build_one_shot_calibration_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    run_dir: Path,
    repo_root: Path | None = None,
) -> Workflow:
    """Compile explicit resources, preflight, physical measurements and publication."""
    if config.workflow != "one-shot-calibration" or config.one_shot is None:
        raise ValueError("calibration plan requires a one-shot-calibration scenario")
    workflow = Workflow("one-shot-calibration")
    topology = add_one_shot_platforms(
        workflow,
        config,
        environment,
        run_dir=run_dir,
        repo_root=repo_root or default_tool_paths().nanofaas_root,
    )
    clock = clock_health_resource(
        endpoints={
            key: endpoint
            for key, endpoint in topology.endpoints.items()
            if topology.resources.nodes[key].config.kind == "edge"
        },
        probes=topology.probes,
        output=run_dir / "clock.jsonl",
    )
    measure = MeasureServiceTask(topology, config.one_shot, run_dir, clock=clock)
    capacity = ValidateCapacityTask(topology, config.one_shot, run_dir, clock=clock)
    workflow.add(
        Steps(
            title="Independent one-shot service calibration",
            steps=(
                CollectTopologyTask(
                    topology.resources,
                    endpoints=topology.endpoints,
                    probes=topology.probes,
                    distribution=topology.distribution,
                    function_settings=topology.function_settings,
                    output=run_dir / "preflight.json",
                ),
                WarmupTask(topology, config.one_shot, run_dir, clock=clock),
                measure,
                capacity,
                PublishCalibrationTask(topology, config.one_shot, run_dir, clock=clock),
            ),
        ),
        requires=(*topology.requires, clock),
    )
    return workflow
