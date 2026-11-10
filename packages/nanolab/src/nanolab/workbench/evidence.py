"""Assess Workbench collection and retain VMs whose raw evidence is unconfirmed."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast, override

from sonata_engine import Members, Resource, Task, TaskInputs, TaskOutcome

from nanolab.config.one_shot import OneShotConfig, OneShotLogs, operational_limit
from nanolab.tasks.one_shot.logging import (
    LogSession,
    collection_evidence,
    export_collector,
    retain_collector_for_recovery,
)
from nanolab.workbench.components import Deployed, collect_deployed
from nanolab.workbench.inventory import Instance
from nanolab.workbench.playbook import WorkbenchHost

DRAIN_POLLS = 5
DRAIN_POLL_SECONDS = 2


def container_identity(inspected: dict[str, Any], name: str) -> dict[str, Any]:
    """Use the same image and resource identity for live and archived evidence."""
    return {
        "container": name,
        "image": inspected["Image"],
        "imageReference": inspected["Config"]["Image"],
        "cpuset": inspected["HostConfig"].get("CpusetCpus") or None,
        "nanoCpus": inspected["HostConfig"].get("NanoCpus"),
        "memoryBytes": inspected["HostConfig"].get("Memory"),
    }


@dataclass
class CollectorEvidence(Task[None]):
    """Drain, collect and verify one collector before its VM can be released."""

    host: WorkbenchHost
    instance: Instance
    resource: Resource[Deployed]
    run_dir: Path
    vars_dir: Path
    storage_dir: str
    logs: OneShotLogs
    settings: OneShotConfig | None
    budget: Callable[[], dict[str, Any]]
    title: str = field(init=False)
    idempotent = False

    def __post_init__(self) -> None:
        """Name the host-specific collection step."""
        self.title = f"Collect the {self.host.key} log collector"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        node = self.host.node
        if node is None:
            raise ValueError("collector recovery requires a VM")
        value = cast(Members, inputs.upstream()).of(self.resource)
        workbench_dir = self.run_dir / "workbench"
        collected = workbench_dir / self.host.key / self.instance.name
        name = f"wb-{self.instance.name}"
        issues: list[str] = []

        def collect(mode: str) -> None:
            instance = self.instance.model_copy(
                update={
                    "config": {
                        **self.instance.config,
                        "collect_mode": mode,
                        "stop_timeout": int(
                            operational_limit(
                                self.settings, "process_drain_seconds", 15
                            )
                        ),
                    }
                }
            )
            collect_deployed(
                self.host,
                instance,
                value,
                vars_dir=self.vars_dir,
                collect_dir=workbench_dir,
            ).run(inputs)

        final_metrics = ""
        try:
            for _ in range(DRAIN_POLLS):
                time.sleep(DRAIN_POLL_SECONDS)
                collect("metrics")
                final_metrics = (collected / "metrics.txt").read_text()
                if collection_evidence(final_metrics)["status"] == "drained":
                    break
        except Exception as error:
            issues.append(f"collector metrics: {error}")
        receipt_current = False
        try:
            # An old success must not authorize deletion after a transport failure.
            (collected / "collection.json").unlink(missing_ok=True)
            receipt_current = True
            collect("final")
        except Exception as error:
            # A failed metrics endpoint must not hide a successful raw recovery.
            issues.append(f"final collection: {error}")

        primary = None
        archive_issue = None
        try:
            if not receipt_current:
                raise RuntimeError(
                    "could not invalidate the previous collection receipt"
                )
            receipt = json.loads((collected / "collection.json").read_text())
            if receipt.get("containerEvidenceError"):
                issues.append(
                    f"container diagnostics: {receipt['containerEvidenceError']}"
                )
            if receipt.get("mode") != "final" or receipt.get("quiesced") is not True:
                raise ValueError("collector writers are not confirmed stopped")
            directory = self.run_dir / "logging" / self.host.key
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "collector-state.tar.gz"
            partial = target.with_suffix(".partial")
            shutil.copyfile(collected / "storage.tar.gz", partial)
            with partial.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != receipt.get("archiveSha256"):
                raise ValueError("collector state archive checksum mismatch")
            partial.replace(target)
            if receipt.get("metricsAvailable") is True:
                final_metrics = (collected / "metrics.txt").read_text()
        except Exception as error:
            archive_issue = str(error)
            retain_collector_for_recovery(
                node, run_dir=self.run_dir, storage=self.storage_dir, error=error
            )
            primary = RuntimeError(
                f"{self.host.key}: collector state archive failed; {self.storage_dir} "
                f"retained on the VM: {error}"
            )
        try:
            diagnostics = json.loads(
                (collected / "container-evidence.json").read_text()
            )
            issues.extend(
                f"container diagnostics: {key}={diagnostics.get(key)}"
                for key in ("logsExitCode", "statsExitCode")
                if diagnostics.get(key) != 0
            )
        except Exception as error:
            issues.append(f"container diagnostics: {error}")
        identity = {}
        try:
            identity = {
                **container_identity(
                    json.loads((collected / "container.json").read_text()), name
                ),
                **self.budget(),
            }
        except Exception as error:
            issues.append(f"container identity: {error}")
        try:
            export_collector(
                node=node,
                name=name,
                run_dir=self.run_dir,
                logs=self.logs,
                session=LogSession(
                    self.host.key,
                    name,
                    self.storage_dir,
                    int((value.deployed_at or 0) * 1e9),
                    identity,
                ),
                final_metrics=final_metrics,
                archive_issue=archive_issue,
                issues=issues,
                container_evidence=collected,
            )
        except Exception as error:
            if primary is None:
                raise
            primary.add_note(f"collector export: {error}")
        if primary is not None:
            raise primary
        return TaskOutcome()
