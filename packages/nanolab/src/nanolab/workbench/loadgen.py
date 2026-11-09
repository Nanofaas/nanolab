"""The one-shot k6 generator as a workbench `loadgen` job on the nanolab host.

The run directory is the job's `/out`: k6 reads `schedule.json` from it and
writes `generator.jsonl` / `generator.stderr` into it, where the evidence
collector tails them as before. A loadgen runs once, so every acquisition
deploys a new instance.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from sonata_engine import Resource, TaskInputs

from nanolab.config.one_shot import operational_limit
from nanolab.tasks.resource_tasks import acquire_now, release_now, resource_of
from nanolab.workbench.components import Deployed, component_resource
from nanolab.workbench.inventory import Instance
from nanolab.workbench.playbook import WorkbenchHost

GENERATOR_DRAIN_SECONDS = 60
# ponytail: one `docker inspect` per second at most, however often callers poll;
# an exit is noticed up to this late.
POLL_SECONDS = 1.0
INSPECT_SECONDS = 2


@dataclass
class LoadgenRun:
    """A deployed loadgen job, polled like a `Popen` through `docker inspect`.

    `poll()` never raises and never waits long, since supervision checks call
    it under a lock: a failed inspect reads as "still running" (None), and
    its cause stays in `error` until an inspect succeeds again.
    """

    deployed: Deployed
    grace: float
    returncode: int | None = None
    checked: float = field(default=0.0, repr=False)
    error: str | None = field(default=None, repr=False)

    @property
    def container(self) -> str:
        """The job's container name."""
        return f"wb-{self.deployed.instance}"

    def poll(self) -> int | None:
        """Return None while the container runs (or is unknown), its exit after."""
        now = time.monotonic()
        if self.returncode is None and now - self.checked >= POLL_SECONDS:
            self.checked = now
            try:
                status, code = subprocess.run(
                    (
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Status}} {{.State.ExitCode}}",
                        self.container,
                    ),
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=INSPECT_SECONDS,
                ).stdout.split()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.error = f"docker inspect {self.container}: {error}"
                return None
            self.error = None
            if status in ("exited", "dead"):
                self.returncode = int(code)
        return self.returncode

    def stop(self) -> None:
        """Stop k6 within the grace, keeping the container for its exit code."""
        self.checked = 0.0
        if self.poll() is None:
            subprocess.run(
                ("docker", "stop", "-t", str(math.ceil(self.grace)), self.container),
                capture_output=True,
                check=True,
                timeout=self.grace + 30,
            )
            self.checked = 0.0
            self.poll()


def loadgen_resource(
    host: WorkbenchHost,
    *,
    run_dir: Path,
    vus: int,
    duration: float,
    settings: Any = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[LoadgenRun]:
    """Own one bounded k6 run; the release stops it and undeploys it."""
    if host.node is not None:
        raise ValueError("the loadgen runs on the nanolab host (ruling 3)")
    run_dir = run_dir.resolve()
    vars_dir = run_dir / "workbench" / "vars"
    grace = operational_limit(settings, "process_exit_seconds", 5)

    def component(name: str, config: dict[str, Any]) -> Resource[Deployed]:
        return component_resource(
            host,
            Instance(name=name, component="loadgen"),
            url=None,
            vars_dir=vars_dir,
            installed=None,
            config_from_inputs=lambda _inputs: config,
        )

    def acquire(inputs: TaskInputs) -> LoadgenRun:
        remaining = duration
        if getattr(settings, "operational_limits", None) is not None:
            manifest = json.loads((run_dir / "manifest.json").read_text())
            grid = manifest["parameters"]["logicalSchedule"]
            remaining = max(
                0,
                grid["anchorMonotonicSeconds"]
                + grid["epochCount"] * manifest["period_seconds"]
                - time.monotonic(),
            )
        drain = operational_limit(
            settings, "generator_drain_seconds", GENERATOR_DRAIN_SECONDS
        )
        # k6 appends to its log file: start it empty, as the old child did.
        (run_dir / "generator.jsonl").write_text("")
        config = {
            "script": "one-shot.js",
            "output_dir": str(run_dir),
            "network": "host",
            "user": f"{os.getuid()}:{os.getgid()}",
            "env": {
                "ONE_SHOT_SCHEDULE": "/out/schedule.json",
                "ONE_SHOT_VUS": str(vus),
                "ONE_SHOT_DURATION": f"{math.ceil(remaining + drain)}s",
            },
        }
        name = f"loadgen-{uuid4().hex[:12]}"
        return LoadgenRun(acquire_now(component(name, config), inputs), grace)

    def release(inputs: TaskInputs, run: LoadgenRun) -> None:
        try:
            run.stop()
        finally:
            # Undeploy reads the config recorded in the `Deployed`.
            release_now(component(run.deployed.instance, {}), inputs, run.deployed)

    return resource_of(
        "Own bounded one-shot k6 generator",
        acquire,
        release,
        requires=requires,
        always_release=True,
        revive=lambda raw: LoadgenRun(Deployed(**raw["deployed"]), raw["grace"]),
    )
