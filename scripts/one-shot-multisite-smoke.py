#!/usr/bin/env python3
"""Acquire a frozen one-shot topology on its placed sites, run preflight, release.

A short integration check before a full observation: VMs on every site and
Azure, forwarders, bootstrap, per-architecture images, control planes, per-site
logging, then the preflight's physical checks (peer graph through the
forwarders, clocks, bandwidth, replica relay). With `--hold` the topology stays
up under the campaign supervisor for that long, so its periodic checks (log
backend, routes, collectors, control planes) run as in an observation. No load
is generated; Sonata releases everything afterwards, on success and on failure.
"""

import argparse
import json
import time
from pathlib import Path

import yaml
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.one_shot.supervision import CampaignSupervision
from nanolab.one_shot.views import select_presentation
from nanolab.plans.one_shot_common import add_one_shot_platforms
from nanolab.tasks.one_shot.clock import clock_health_resource
from nanolab.tasks.one_shot.preflight import CollectTopologyTask
from sonata_engine import Steps, Task, TaskInputs, TaskOutcome, Workflow


class HoldTask(Task[None]):
    """Keep every acquired resource up while the supervisor checks them."""

    title = "Hold the topology under supervision"

    def __init__(self, seconds: float) -> None:
        """Hold for `seconds`."""
        self.seconds = seconds

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Sleep; a failed check interrupts the sleep and fails the run."""
        time.sleep(self.seconds)
        return TaskOutcome()


def main(argv=None):
    """Run the smoke check for one prepared freeze directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("freeze", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--hold", type=float, default=0, help="seconds")
    args = parser.parse_args(argv)
    frozen = json.loads((args.freeze / "freeze.json").read_text())
    config = ScenarioConfig.model_validate(
        yaml.safe_load((args.freeze / "scenario.yaml").read_text())
    )
    environment = EnvironmentConfig.model_validate(
        yaml.safe_load((args.freeze / "environment.yaml").read_text())
    )
    settings = config.one_shot
    if settings is None:
        raise ValueError("scenario has no one-shot settings")
    args.output.mkdir(parents=True, exist_ok=False)
    supervision = CampaignSupervision(
        args.output,
        presentation_factory=lambda fail: select_presentation(
            "console", on_failure=fail
        ),
    )
    workflow = Workflow("one-shot multi-site smoke")
    topology = add_one_shot_platforms(
        workflow,
        config,
        environment,
        run_dir=args.output,
        repo_root=Path(frozen["source"]),
        terminal_queueing=True,
        max_retries=3,
        observer_bootstrap=True,
        supervision=supervision,
    )
    clock = clock_health_resource(
        endpoints=topology.endpoints,
        probes=topology.probes,
        reporting_nodes=set(topology.neighbor_graph),
        output=args.output / "clock.jsonl",
        protocol_version=2,
        settings=settings,
    )
    preflight = CollectTopologyTask(
        topology.resources,
        endpoints=topology.endpoints,
        probes=topology.probes,
        distribution=topology.distribution,
        node_distributions=topology.node_distributions,
        function_settings=topology.function_settings,
        collectors=topology.collectors,
        neighbor_graph=topology.neighbor_graph,
        node_function_settings=topology.node_function_settings,
        purpose=settings.purpose,
        strict_memory=True,
        require_no_swap=False,
        settings=settings,
        protocol_version=2,
        output=args.output / "preflight.json",
    )
    steps = (preflight, HoldTask(args.hold)) if args.hold else (preflight,)
    workflow.add(
        Steps(title="Multi-site preflight", steps=steps),
        requires=(*topology.requires, clock),
    )
    supervision.run(workflow.run)  # raises the run's primary failure
    print(json.dumps({"smoke": "passed", "output": str(args.output)}))


if __name__ == "__main__":
    main()
