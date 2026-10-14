#!/usr/bin/env python3
"""Exercise owned Loki/Grafana ingestion and cleanup via Sonata, without VMs."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import time
from pathlib import Path

from nanolab.config.one_shot import OneShotLogs
from nanolab.one_shot.supervision import CampaignSupervision
from nanolab.tasks.one_shot.logging import backend_resource
from sonata_engine import Task, TaskOutcome, Workflow


class ObserveIngestion(Task[None]):
    title = "Observe real Loki ingestion across background probes"

    def __init__(self, resource, output, seconds):
        self.resource, self.output, self.seconds = resource, output, seconds

    def run(self, inputs):
        inputs.resource(self.resource)
        # Include the WAL's ten-second disk-monitor tick; no traffic or VM needed.
        time.sleep(self.seconds)
        rows = [
            json.loads(line)
            for line in (self.output / "logging/backend-probes.jsonl")
            .read_text()
            .splitlines()
        ]
        if len(rows) < 2 or any(row["state"] != "passed" for row in rows):
            raise RuntimeError(
                "Loki smoke did not observe sustained write/read success"
            )
        (self.output / "smoke.json").write_text(
            json.dumps(
                {
                    "state": "passed",
                    "probeCount": len(rows),
                    "seconds": self.seconds,
                    "scope": "owned local backend only; no VMs or function requests",
                },
                indent=2,
            )
            + "\n"
        )
        return TaskOutcome()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--host-address", required=True, help="Existing private VM-facing host address"
    )
    parser.add_argument("--seconds", type=float, default=15)
    args = parser.parse_args()
    if args.seconds < 12:
        parser.error("Use at least 12 seconds to include the WAL monitor tick")
    root, output = args.runtime_source.resolve(), args.output.resolve()
    archive = output.with_name(output.name + "-archive")
    if output.exists() or archive.exists():
        raise FileExistsError("Use new smoke output and archive paths")
    # Use existing pinned images only, without an implicit image pull.
    for image in ("grafana/loki:3.7.8", "grafana/grafana:13.2.3"):
        subprocess.run(
            ["docker", "image", "inspect", image], check=True, capture_output=True
        )
    with socket.socket() as listener:
        listener.bind((args.host_address, 0))
        port = listener.getsockname()[1]
    logs = OneShotLogs.model_validate(
        {
            "enabled": True,
            "endpoint": f"http://{args.host_address}:{port}/loki/api/v1/push",
            "backendOwned": True,
            "backendBind": args.host_address,
        }
    )
    output.mkdir(parents=True)
    supervisor = CampaignSupervision(output, archive=archive)
    backend = backend_resource(logs, root=root, run_dir=output, supervision=supervisor)
    workflow = Workflow("one-shot-logging-smoke")
    workflow.add(ObserveIngestion(backend, output, args.seconds), requires=(backend,))
    supervisor.run(workflow.run)


if __name__ == "__main__":
    main()
