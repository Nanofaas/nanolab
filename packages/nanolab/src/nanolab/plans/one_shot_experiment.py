"""Sequential isolated baseline/ORACLE/EWMA comparisons on identical resources."""

from __future__ import annotations

import random
from pathlib import Path

from sonata_engine import Steps, Workflow

from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.one_shot import TimingCell
from nanolab.functions.catalog import resolve_function_definition
from nanolab.plans.one_shot_common import add_one_shot_platforms
from nanolab.tasks.one_shot.clock import clock_health_resource
from nanolab.tasks.one_shot.experiment import (
    CollectEvidenceTask,
    EvaluateOneShotTask,
    FreezeManifestTask,
    RunOneShotLoadTask,
    UploadForecastTask,
    evidence_collector_resource,
    generator_resource,
    read_qualification,
)
from nanolab.tasks.one_shot.preflight import CollectTopologyTask
from nanolab.tasks.one_shot.qualification import read_profile
from nanolab.tasks.one_shot.trace import TraceArtifact
from nanolab.workspace.paths import default_tool_paths


def build_one_shot_experiment_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    run_dir: Path,
    repo_root: Path | None = None,
) -> Workflow:
    """Validate immutable prerequisites before composing any owned VM or load."""
    if (
        config.workflow != "one-shot-experiment"
        or config.one_shot is None
        or config.one_shot.experiment is None
    ):
        raise ValueError("experiment requires its dedicated one-shot configuration")
    settings = config.one_shot
    experiment = settings.experiment
    if experiment is None:
        raise ValueError("experiment settings are required")
    profile, qualification = read_profile(settings), read_qualification(settings)
    source = repo_root or default_tool_paths().nanofaas_root
    aliases = {
        key: resolve_function_definition(key, source).family for key in config.functions
    }
    edges = [node.id for node in settings.nodes if node.kind == "edge"]
    trace = TraceArtifact(
        nodes=edges,
        functions=list(aliases.values()),
        period_seconds=qualification.period_seconds,
        flow_quantum=settings.flow_quantum,
        seed=settings.seed,
        payloads={aliases[key]: fn.input for key, fn in settings.functions.items()},
        windows=[
            {
                node: {aliases[key]: rate for key, rate in rates.items()}
                for node, rates in window.rates.items()
            }
            for window in experiment.windows
        ],
    )
    maximum = max(
        sum(
            sum(rates.values())
            for rates in TimingCell.model_validate(cell["cell"]).rates.values()
        )
        for cell in qualification.matrix
    )
    if any(
        sum(sum(rates.values()) for rates in window.values()) > maximum
        for window in trace.windows
    ):
        raise ValueError("experiment load exceeds the independently qualified matrix")
    workflow = Workflow("one-shot-experiment")
    for repetition in range(experiment.repetitions):
        modes = ["baseline", "oracle", "ewma"]
        # Reproducible mode order, not a security-sensitive random choice.
        random.Random(settings.seed + repetition).shuffle(modes)  # nosec B311
        for mode in modes:
            directory = run_dir / f"rep-{repetition}-{mode}"
            topology = add_one_shot_platforms(
                workflow,
                config,
                environment,
                run_dir=directory,
                repo_root=source,
                forecast_provider="EWMA" if mode == "ewma" else "ORACLE",
                terminal_queueing=True,
                baseline=mode == "baseline",
            )
            clock = clock_health_resource(
                endpoints={key: topology.endpoints[key] for key in edges},
                probes=topology.probes,
                output=directory / "clock.jsonl",
            )
            requires = (*topology.requires, clock)
            preflight = CollectTopologyTask(
                topology.resources,
                endpoints=topology.endpoints,
                probes=topology.probes,
                distribution=topology.distribution,
                function_settings=topology.function_settings,
                output=directory / "preflight.json",
            )
            freeze = FreezeManifestTask(
                topology,
                settings,
                profile,
                qualification,
                trace,
                mode=mode,
                repetition=repetition,
                modes=modes,
                clock=clock,
                run_dir=directory,
            )
            workflow.add(
                Steps(
                    title=f"Verify and freeze {mode} comparison",
                    steps=(preflight, freeze),
                ),
                requires=requires,
            )
            workflow.add(UploadForecastTask(freeze), requires=requires)
            generator = generator_resource(
                run_dir=directory,
                vus=experiment.generator_vus,
                duration=len(trace.windows) * trace.period_seconds + 25,
            )
            collector = evidence_collector_resource(freeze, generator)
            workflow.add(
                RunOneShotLoadTask(freeze, generator, collector),
                requires=(*requires, generator, collector),
            )
            workflow.add(
                Steps(
                    title=f"Evaluate {mode} evidence",
                    steps=(CollectEvidenceTask(freeze), EvaluateOneShotTask()),
                ),
                requires=requires,
            )
    return workflow
