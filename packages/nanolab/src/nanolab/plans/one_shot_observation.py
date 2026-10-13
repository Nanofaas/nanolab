"""Compose the observation using the existing Sonata ownership graph."""

import json
from pathlib import Path
from types import SimpleNamespace

from sonata_engine import Steps, Workflow

from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.one_shot.models import CampaignManifest
from nanolab.one_shot.supervision import (
    CampaignSupervision,
    check_background_worker,
    check_sampler,
)
from nanolab.one_shot.vm_resources import (
    vm_telemetry_resource,
)
from nanolab.plans.one_shot_common import add_one_shot_platforms
from nanolab.tasks.one_shot.arrivals import read_frozen_arrival_profile
from nanolab.tasks.one_shot.artifacts import write_immutable_artifact
from nanolab.tasks.one_shot.clock import clock_health_resource
from nanolab.tasks.one_shot.experiment import (
    RunOneShotLoadTask,
    evidence_collector_resource,
    generator_resource,
    generator_rows,
    logical_schedule,
)
from nanolab.tasks.one_shot.observation import (
    ConfigureOracle,
    ObservationFreeze,
    ObservationReport,
    ObserverPrecheck,
    WarmInstances,
    oracle_lifetime_seconds,
)
from nanolab.tasks.one_shot.preflight import (
    CollectTopologyTask,
)
from nanolab.tasks.one_shot.trace import (
    TraceArtifact,
)


def build_one_shot_observation_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    freeze_dir: Path,
    run_dir: Path,
    supervision: CampaignSupervision,
) -> Workflow:
    """Build without acquisition; Sonata owns all runtime resources and release."""
    frozen = json.loads((freeze_dir / "freeze.json").read_text())
    protocol = json.loads((freeze_dir / "operator-protocol.json").read_text())
    settings = config.one_shot
    assert (
        settings is not None
        and settings.timing is not None
        and settings.experiment is not None
        and settings.operational_limits is not None
    )
    arrival_profile = read_frozen_arrival_profile(
        freeze_dir,
        frozen,
        warmup_seconds=settings.experiment.warmup_seconds,
    )
    if arrival_profile is not None:
        write_immutable_artifact(
            run_dir / "arrival-profile.json",
            (freeze_dir / "arrival-profile.json").read_bytes(),
        )
    workflow = Workflow("first-functional-oracle-observation")
    topology = add_one_shot_platforms(
        workflow,
        config,
        environment,
        run_dir=run_dir,
        repo_root=Path(frozen["source"]),
        forecast_provider="ORACLE",
        terminal_queueing=True,
        observer_bootstrap=True,
        supervision=supervision,
        forecast_max_age_seconds=protocol["oracleMaxAgeSeconds"],
    )
    clock = clock_health_resource(
        endpoints=topology.endpoints,
        probes=topology.probes,
        reporting_nodes=set(topology.neighbor_graph),
        output=run_dir / "clock.jsonl",
        protocol_version=2,
        settings=settings,
    )
    clock = supervision.resource(clock, "clock", check_background_worker)
    trace = TraceArtifact.model_validate_json((freeze_dir / "trace.json").read_text())
    run = SimpleNamespace(
        topology=topology,
        settings=settings,
        trace=trace,
        clock=clock,
        run_dir=run_dir,
        mode="oracle",
        repetition=0,
        frozen=frozen,
        frozen_dir=freeze_dir,
        supervision=supervision,
        qualification=SimpleNamespace(
            protocol=settings.timing.protocol.model_dump(by_alias=True),
            epsilon=protocol["maxOperationalFraction"],
            margin_seconds=0,
            qualified=False,
        ),
    )
    preflight = CollectTopologyTask(
        topology.resources,
        endpoints=topology.endpoints,
        probes=topology.probes,
        distribution=topology.distribution,
        function_settings=topology.function_settings,
        collectors=topology.collectors,
        neighbor_graph=topology.neighbor_graph,
        node_function_settings=topology.node_function_settings,
        purpose=settings.purpose,
        strict_memory=True,
        require_no_swap=False,
        settings=settings,
        protocol_version=2,
        output=run_dir / "preflight.json",
    )
    requires = (*topology.requires, clock)
    workflow.add(
        Steps(
            title="Preflight, warm instances, observer cost, frozen schedule",
            steps=(
                preflight,
                WarmInstances(run),
                ObserverPrecheck(run),
                ObservationFreeze(run),
                ConfigureOracle(run),
            ),
        ),
        requires=requires,
    )
    telemetry = supervision.resource(
        vm_telemetry_resource(run, clock=clock),
        "sampler",
        check_sampler,
    )
    generator = generator_resource(
        run_dir=run_dir,
        vus=512,
        duration=oracle_lifetime_seconds(
            trace, warmup_seconds=settings.experiment.warmup_seconds
        )
        - settings.operational_limits.generator_drain_seconds,
        settings=settings,
        telemetry=telemetry,
    )

    generator_finished = False

    def generator_complete():
        nonlocal generator_finished
        if generator_finished:
            return True
        planned = {
            row["originalId"]
            for row in json.loads((run_dir / "schedule.json").read_text())
        }
        events = generator_rows(run_dir / "generator.jsonl")
        emitted = {row["originalId"] for row in events if row["event"] == "emitted"}
        returned = {row["originalId"] for row in events if row["event"] == "result"}
        generator_finished = planned <= emitted and planned <= returned
        return generator_finished

    run.generator_complete = generator_complete

    def generator_health(value):
        manifest = CampaignManifest.model_validate_json(
            (run_dir / "manifest.json").read_bytes()
        )
        _session, anchor = logical_schedule(manifest)
        supervision.check_process(
            value,
            expected_end=anchor + len(trace.windows) * manifest.period_seconds,
            completed=generator_complete,
        )

    generator = supervision.resource(generator, "generator", generator_health)
    collector = supervision.resource(
        evidence_collector_resource(run, generator),
        "collector",
        check_background_worker,
    )
    workflow.add(
        RunOneShotLoadTask(
            run, generator, collector, telemetry, enforce_timing_budget=False
        ),
        requires=(*requires, generator, collector, telemetry),
    )
    workflow.add(ObservationReport(run), requires=requires)
    return workflow
