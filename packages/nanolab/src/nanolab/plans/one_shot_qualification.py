"""Independent measured timing workflow consuming an immutable service profile."""

from pathlib import Path

from sonata_engine import Steps, Workflow

from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.plans.one_shot_common import add_one_shot_platforms
from nanolab.tasks.one_shot.clock import clock_health_resource
from nanolab.tasks.one_shot.preflight import CollectTopologyTask
from nanolab.tasks.one_shot.qualification import QualifyTimingTask, read_profile
from nanolab.workspace.paths import default_tool_paths


def build_one_shot_qualification_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    run_dir: Path,
    repo_root: Path | None = None,
) -> Workflow:
    """Verify prerequisite bytes first, then compose deployed preflight and timing."""
    if config.workflow != "one-shot-qualification" or config.one_shot is None:
        raise ValueError("qualification requires its dedicated one-shot scenario")
    if config.one_shot.timing is None:
        raise ValueError(
            "qualification requires explicit period candidates and timing matrix"
        )
    profile = read_profile(config.one_shot)
    workflow = Workflow("one-shot-qualification")
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
    workflow.add(
        Steps(
            title="Independent one-shot timing qualification",
            steps=(
                CollectTopologyTask(
                    topology.resources,
                    endpoints=topology.endpoints,
                    probes=topology.probes,
                    distribution=topology.distribution,
                    function_settings=topology.function_settings,
                    output=run_dir / "preflight.json",
                ),
                QualifyTimingTask(
                    topology, config.one_shot, profile, clock=clock, run_dir=run_dir
                ),
            ),
        ),
        requires=(*topology.requires, clock),
    )
    return workflow
