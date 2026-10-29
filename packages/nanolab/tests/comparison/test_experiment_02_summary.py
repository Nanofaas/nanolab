"""Experiment 02 summaries: experiment 01's verdicts plus cost per useful response."""

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nanolab.comparison.cells import (
    EVENTS,
    SUMMARY,
    attempt_dir,
    write_cell_result,
)
from nanolab.comparison.experiment_02 import (
    CONFIGURATIONS_BY_KEY,
    SAMPLES,
    SCHEDULER,
    Experiment02Cell,
    required_evidence,
    summarize_experiment_02_cell,
    write_experiment_02_report,
)
from nanolab.config.experiment_02 import Budget, Experiment02Protocol

BUDGET = Budget(cpus=1, memoryMiB=1024)
IDENTITY = {"tag": "recipe-test", "experiment": {"profile": "experiment-02"}}
PROTOCOL = Experiment02Protocol.model_validate(
    {
        "status": "pilot",
        "payloadFile": "x.json",
        "payloadSha256": "0" * 64,
        "idleSeconds": 10,
        "warmup": {"rate": 1, "durationSeconds": 10},
        "steps": [{"rate": 2, "durationSeconds": 10}],
        "httpTimeoutSeconds": 1,
        "completionMarginSeconds": 1,
        "gracefulStopSeconds": 2,
        "drainSeconds": 2,
        "fixedConcurrency": 1,
        "queueCapacity": 1,
        "preAllocatedVUs": 1,
        "maxVUs": 2,
        "criteria": {
            "latencyThresholdMs": 1000,
            "minWithinThresholdFraction": 0.9,
            "maxPendingFraction": 0.1,
        },
        "budgets": [{"cpus": 1, "memoryMiB": 1024}],
        "referenceBudget": {"cpus": 1, "memoryMiB": 1024},
        "readinessTimeoutSeconds": 10,
        "orderSeed": 1,
    }
)
# Origin at 1000 s: idle [1000, 1010), warmup [1010, 1020), step-01 [1020, 1030),
# drain [1030, 1032].
ORIGIN_MS = 1_000_000


def _events(*, correct: bool) -> str:
    lines = [{"t": "origin", "ts": ORIGIN_MS}]
    for phase, start, rate in (("warmup", 1010, 1), ("step-01", 1020, 2)):
        for index in range(rate * 10):
            sent = (start + index / rate) * 1000
            request = f"{phase}-{index}"
            lines.append({"t": "send", "id": request, "phase": phase, "ts": sent})
            lines.append(
                {
                    "t": "done",
                    "id": request,
                    "phase": phase,
                    "ts": sent + 100,
                    "dur": 100,
                    "status": 200,
                    "ok": correct,
                }
            )
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def _samples(skip: tuple[int, ...] = ()) -> str:
    """Half a core and 1000 bytes in the deployment; 100 in the engine, 50 agents."""
    lines = []
    for at in range(995, 1040, 5):
        if at in skip:
            continue
        usage = int((at - 995) * 500_000)
        group = {
            "memoryCurrent": 1000,
            "inactiveFile": 0,
            "cpuUsageUsec": usage,
            "nrThrottled": 0,
            "throttledUsec": 0,
            "oomKill": 0,
        }
        engine = {**group, "memoryCurrent": 100, "cpuUsageUsec": usage // 10}
        cgroups = {
            ".": group,
            "docker.service": engine,
            "containerd.service": {**engine, "memoryCurrent": 0, "cpuUsageUsec": 0},
            "docker-a.scope": group,
            "@observers": {**group, "memoryCurrent": 50, "cpuUsageUsec": 0},
        }
        lines.append(json.dumps({"ts": at, "cgroups": cgroups}))
    return "\n".join(lines) + "\n"


def _scrapes(*, failed_at: int | None = None) -> str:
    return "".join(
        json.dumps({"ts": at, "status": "error" if at == failed_at else "ok"}) + "\n"
        for at in range(995, 1040, 5)
    )


def _measured_cell(
    root: Path,
    *,
    correct: bool = True,
    skip: tuple[int, ...] = (),
    key: str = "nanofaas-p1-jvm",
    scrapes: str | None = None,
) -> Experiment02Cell:
    cell = Experiment02Cell(CONFIGURATIONS_BY_KEY[key], BUDGET, 1)
    selected = attempt_dir(cell.run_dir(root), 1)
    (selected / "metrics").mkdir(parents=True)
    (selected / EVENTS).write_text(_events(correct=correct))
    (selected / SUMMARY).write_text(json.dumps({"metrics": {}}))
    (selected / SAMPLES).write_text(_samples(skip))
    if cell.configuration.platform == "nanofaas":
        (selected / SCHEDULER).write_text(_scrapes() if scrapes is None else scrapes)
    (selected / "readiness.json").write_text(json.dumps({"firstCorrectSeconds": 3.5}))
    write_cell_result(
        cell,
        root,
        identity=IDENTITY,
        attempt=1,
        outcome="measured",
        reason="completed",
        started_at=datetime.now(UTC),
        required=required_evidence(cell.configuration),
    )
    return cell


def _phase(summary: dict, name: str) -> dict:
    return next(row for row in summary["phases"] if row["phase"] == name)


def test_efficiency_includes_drain_and_divides_by_correct_responses(
    tmp_path,
):
    cell = _measured_cell(tmp_path)
    summary = summarize_experiment_02_cell(cell, tmp_path, PROTOCOL)
    step = _phase(summary, "step-01")
    assert step["withinThreshold"] == 20
    # Half a core and 1000 bytes over [20 s, 30 s + 2 s of drain].
    assert (step["efficiencyStartSeconds"], step["efficiencyEndSeconds"]) == (20, 32)
    assert step["cpuSeconds"] == 6.0
    assert step["cpuSecondsPerUseful"] == 0.3
    assert step["memoryByteSecondsPerUseful"] == 600.0
    assert summary["configuration"] == "nanofaas-p1-jvm"
    assert summary["budget"] == "cpu1-mem1024"
    assert summary["readiness"] == {"firstCorrectSeconds": 3.5}


def test_last_efficiency_window_ends_at_calendar_drain_end(tmp_path):
    summary = summarize_experiment_02_cell(_measured_cell(tmp_path), tmp_path, PROTOCOL)
    drain = PROTOCOL.phases()[-1]
    end = drain["startSeconds"] + drain["durationSeconds"]
    assert _phase(summary, "step-01")["efficiencyEndSeconds"] == end
    # Windows overlap: the warmup's runs into step-01.
    assert _phase(summary, "warmup")["efficiencyEndSeconds"] == 22


def test_efficiency_is_unknown_when_only_the_drain_has_a_gap(tmp_path):
    # The step itself ends covered at 1030 s; only its drain, to 1032 s, is not.
    cell = _measured_cell(tmp_path, skip=(1035,))
    step = _phase(summarize_experiment_02_cell(cell, tmp_path, PROTOCOL), "step-01")
    assert step["workingSetBytes"]["coverage"] == "complete"
    assert step["cpuSeconds"] is None
    assert step["cpuSecondsPerUseful"] is None
    assert step["withinThreshold"] == 20


def test_efficiency_is_undefined_without_useful_responses(tmp_path):
    cell = _measured_cell(tmp_path, correct=False)
    step = _phase(summarize_experiment_02_cell(cell, tmp_path, PROTOCOL), "step-01")
    assert step["withinThreshold"] == 0
    assert step["cpuSeconds"] == 6.0
    assert step["cpuSecondsPerUseful"] is None
    assert step["memoryByteSecondsPerUseful"] is None


def test_engine_and_agents_are_reported_separately(tmp_path):
    cell = _measured_cell(tmp_path)
    idle = _phase(summarize_experiment_02_cell(cell, tmp_path, PROTOCOL), "idle")
    assert idle["workingSetBytes"]["mean"] == 1000.0
    assert idle["engineWorkingSetMeanBytes"] == 100.0
    assert idle["observersWorkingSetMeanBytes"] == 50.0
    assert idle["containersMax"] == 1
    assert idle["coverage"] == {
        "deployment": "complete",
        "engine": "complete",
        "observers": "complete",
    }


def test_gap_invalidates_resource_means_and_maxima(tmp_path):
    cell = _measured_cell(tmp_path, skip=(1010, 1015))
    warmup = _phase(summarize_experiment_02_cell(cell, tmp_path, PROTOCOL), "warmup")
    assert warmup["workingSetBytes"]["mean"] is None
    assert warmup["workingSetBytes"]["max"] is None
    assert warmup["workingSetBytes"]["coverage"] == "gap"
    assert warmup["containersMax"] is None
    # The ledger is intact, so the service values stand.
    assert warmup["withinThreshold"] == 10


def test_invalid_idle_coverage_makes_resource_deltas_unknown(tmp_path):
    cell = _measured_cell(tmp_path, skip=(1000, 1005))
    step = _phase(summarize_experiment_02_cell(cell, tmp_path, PROTOCOL), "step-01")
    assert step["workingSetBytes"]["mean"] == 1000.0
    assert step["workingSetDeltaFromIdle"] is None


def test_scheduler_gap_is_partial_and_direct_reference_is_not_applicable(tmp_path):
    complete = summarize_experiment_02_cell(
        _measured_cell(tmp_path / "a"), tmp_path / "a", PROTOCOL
    )
    assert complete["schedulerCoverage"] == "complete"
    assert complete["schedulerSamples"] == SCHEDULER
    failed = summarize_experiment_02_cell(
        _measured_cell(tmp_path / "b", scrapes=_scrapes(failed_at=1015)),
        tmp_path / "b",
        PROTOCOL,
    )
    assert failed["schedulerCoverage"] == "partial"
    direct = summarize_experiment_02_cell(
        _measured_cell(tmp_path / "c", key="direct-reference"), tmp_path / "c", PROTOCOL
    )
    assert (direct["schedulerCoverage"], direct["schedulerSamples"]) == (
        "not_applicable",
        None,
    )


def test_report_lists_incomplete_cells_without_values(tmp_path):
    measured = _measured_cell(tmp_path)
    missing = Experiment02Cell(CONFIGURATIONS_BY_KEY["direct-reference"], BUDGET, 1)
    path = write_experiment_02_report(tmp_path, (measured, missing), PROTOCOL)
    summaries = json.loads((tmp_path / "experiment-02-summary.json").read_text())
    assert [row["outcome"] for row in summaries] == ["measured", "incomplete"]
    assert summaries[1]["phases"] == []
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    incomplete = [row for row in rows if row["outcome"] == "incomplete"]
    assert len(incomplete) == 1
    assert incomplete[0]["configuration"] == "direct-reference"
    assert incomplete[0]["cpuSecondsPerUseful"] == ""
    step = next(row for row in rows if row["phase"] == "step-01")
    assert float(step["cpuSecondsPerUseful"]) == 0.3
    assert step["deploymentCoverage"] == "complete"
    assert step["schedulerCoverage"] == "complete"


def test_scrape_errors_before_the_calendar_do_not_make_coverage_partial(tmp_path):
    # The sampler starts before the platform: its first scrapes are refused.
    cell = _measured_cell(tmp_path, scrapes=_scrapes(failed_at=995))
    summary = summarize_experiment_02_cell(cell, tmp_path, PROTOCOL)
    assert summary["schedulerCoverage"] == "complete"


def test_experiment02_uses_all_correct_responses_for_latency_and_efficiency(tmp_path):
    cell = _measured_cell(tmp_path)
    selected = attempt_dir(cell.run_dir(tmp_path), 1)
    lines = [{"t": "origin", "ts": ORIGIN_MS}]
    for index, (kind, duration, correct) in enumerate(
        [
            ("done", 20, True),
            ("done", 2000, True),
            ("done", 1, False),
            ("error", 25, False),
            ("timeout", 1000, False),
            (None, 0, False),
        ]
    ):
        sent = 1_020_000 + index * 1000
        lines.append({"t": "send", "id": str(index), "phase": "step-01", "ts": sent})
        if kind is not None:
            lines.append(
                {
                    "t": kind,
                    "id": str(index),
                    "phase": "step-01",
                    "ts": sent + duration,
                    "dur": duration,
                    "ok": correct,
                    "status": 200,
                }
            )
    (selected / EVENTS).write_text("\n".join(json.dumps(v) for v in lines) + "\n")
    summary = summarize_experiment_02_cell(cell, tmp_path, PROTOCOL)
    row = _phase(summary, "step-01")
    assert row["sent"] == 6
    assert row["correct"] == 2
    assert row["incorrect"] == 1
    assert row["correctThroughput"] == 0.2
    assert row["successFraction"] == pytest.approx(2 / 6)
    assert row["p50Ms"] == 20
    assert row["p99Ms"] == 2000
    assert row["latencyPopulation"] == "correct responses of the send cohort"
    assert row["errors"] == row["timeouts"] == row["pending"] == 1
    assert row["cpuSecondsPerUseful"] == 3.0
    assert row["memoryByteSecondsPerUseful"] == 6000.0
    assert "sustainability" not in row
    assert "verdict" not in row["load"]
    assert summary["serviceDefinition"] == "correct-response"


def test_correct_only_protocol_has_no_fabricated_timely_fraction(tmp_path):
    protocol = PROTOCOL.model_copy(update={"criteria": None})
    row = _phase(
        summarize_experiment_02_cell(_measured_cell(tmp_path), tmp_path, protocol),
        "step-01",
    )
    assert row["withinThreshold"] is None
    assert row["withinThresholdFraction"] is None
    assert row["successFraction"] == 1
    assert row["p99Ms"] == 100


@pytest.mark.parametrize(
    "scrapes", ["{}\n", "null\n", '{"ts":"bad"}\n', '{"ts":NaN,"status":"ok"}\n']
)
def test_malformed_scheduler_sample_keeps_service_and_marks_telemetry_partial(
    tmp_path, scrapes
):
    cell = _measured_cell(tmp_path, scrapes=scrapes)
    summary = summarize_experiment_02_cell(cell, tmp_path, PROTOCOL)
    assert summary["schedulerCoverage"] == "partial"
    assert _phase(summary, "step-01")["successFraction"] == 1
