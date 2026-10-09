"""Owned, paced handler contention with independent SDK occupancy observations."""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from nanolab.tasks.one_shot.artifacts import canonical_bytes, write_immutable_artifact
from nanolab.tasks.one_shot.trace import TraceArtifact, request_schedule
from nanolab.tasks.one_shot.verification import validate_proof


def audit_load(*, schedule, rows, proofs, cutoff, max_lateness):
    """Require paced emissions and independent SDK handler occupancy."""
    planned = {
        row["originalId"]: row for row in schedule if row["scheduledAt"] <= cutoff
    }
    emitted = [
        row
        for row in rows
        if row["event"] == "emitted" and row["originalId"] in planned
    ]
    returned = {
        row["originalId"]: row
        for row in rows
        if row["event"] == "result" and row["originalId"] in planned
    }
    ids = [row["originalId"] for row in emitted]
    valid = bool(
        planned
        and len(ids) == len(set(ids)) == len(planned)
        and set(ids) == set(returned) == set(planned)
    )
    lateness = [
        row["emittedAt"] - planned[row["originalId"]]["scheduledAt"] for row in emitted
    ]
    valid = (
        valid
        and bool(lateness)
        and min(lateness) >= -0.001
        and max(lateness) <= max_lateness
    )
    occupied = {}
    for proof in proofs:
        outcome = returned.get(proof["originalId"], {})
        execution = outcome.get("terminalExecutionId") or outcome.get("executionId")
        if (
            validate_proof(proof, execution)
            and proof.get("state") == "RELEASED"
            and proof.get("handlerStarted")
            and proof.get("occupancySeconds", 0) > 0
        ):
            occupied[proof["originalId"]] = proof
    groups = {(row["origin"], row["function"]) for row in planned.values()}
    observed = {(row["origin"], row["function"]) for row in occupied.values()}
    valid = valid and groups <= observed
    return {
        "valid": bool(valid),
        "planned": len(planned),
        "emitted": len(emitted),
        "returned": len(returned),
        "generatorDeficit": len(planned) - len(set(ids)),
        "httpErrors": sum(
            row.get("statusCode", 0) >= 400
            or str(row.get("responseStatus", "")).upper() == "ERROR"
            for row in returned.values()
        ),
        "physicalCompletions": len(occupied),
        "physicalOccupancySeconds": sum(
            row["occupancySeconds"] for row in occupied.values()
        ),
        "physicalGroups": [list(pair) for pair in sorted(observed)],
        "maxArrivalLatenessSeconds": max(lateness) if lateness else None,
        "measuredThrough": cutoff,
        "method": "owned-http-sdk-contention-v1",
        "outsideMeasuredPrefixOriginals": len(schedule) - len(planned),
    }


class QualificationLoad:
    """A task-local lifecycle owns k6 and collector even when preparation fails."""

    def __init__(
        self, topology, settings, inputs, *, cell, period, run_dir: Path, clock
    ):
        """Bind the cell, physical topology and future absolute load schedule."""
        self.inputs, self.clock, self.run_dir = inputs, clock, run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run = SimpleNamespace(topology=topology, run_dir=run_dir)
        duration = period * (settings.timing.minimum_samples * 2 + 2)
        rates = {
            node: {
                topology.function_aliases[name]: rate for name, rate in values.items()
            }
            for node, values in cell.rates.items()
        }
        trace = TraceArtifact(
            nodes=list(rates),
            functions=list(topology.function_settings),
            period_seconds=duration,
            flow_quantum=settings.flow_quantum,
            seed=settings.seed,
            payloads={
                name: fn.input for name, fn in topology.function_settings.items()
            },
            windows=[rates],
        )
        self.schedule = request_schedule(
            trace,
            anchor=datetime.now(UTC) + timedelta(seconds=2),
            endpoints={
                node: inputs.resource(topology.endpoints[node]) for node in rates
            },
            run_id="qualification:" + cell.id,
        )
        write_immutable_artifact(
            run_dir / "schedule.json", canonical_bytes(self.schedule)
        )
        self.duration, self.stack = duration, ExitStack()

    def __enter__(self):
        """Start owned processes and await independently proven handler activity."""
        from nanolab.tasks.one_shot.experiment import (
            LiveEvidenceCollector,
            generator_resource,
        )

        try:
            resource = generator_resource(
                run_dir=self.run_dir, vus=64, duration=self.duration
            )
            self.process = resource.acquire(self.inputs)
            self.stack.callback(resource.release, self.inputs, self.process)
            self.collector = LiveEvidenceCollector(self.run, self.inputs)
            self.stack.callback(self.collector.close)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                self.require_healthy()
                if self.audit(cutoff=time.time() - 0.5)["valid"]:
                    return self
                self.clock.require_healthy()
                time.sleep(0.25)
            raise RuntimeError(
                "qualification workload has no accounted SDK handler contention"
            )
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise

    def require_healthy(self):
        """Reject a stopped generator or collector during timing measurements."""
        if self.process.poll() is not None:
            raise RuntimeError("qualification generator stopped during measurements")
        self.collector.require_healthy()
        self.clock.require_healthy()

    def audit(self, *, cutoff):
        """Persist the accounted measurement prefix and independent SDK occupancy."""
        from nanolab.tasks.one_shot.experiment import generator_rows, physical_attempts

        result = audit_load(
            schedule=self.schedule,
            rows=generator_rows(self.run_dir / "generator.jsonl"),
            proofs=physical_attempts(self.run_dir / "physical.jsonl"),
            cutoff=cutoff,
            max_lateness=0.25,
        )
        (self.run_dir / "load-summary.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
        return result

    def __exit__(self, kind, error, traceback):
        """Release every owned resource while retaining the primary task error."""
        try:
            self.stack.close()
        except Exception as secondary:
            if error is None:
                raise
            error.add_note(
                "Secondary qualification-load cleanup failure: " + str(secondary)
            )
