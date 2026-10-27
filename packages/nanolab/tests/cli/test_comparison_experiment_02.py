"""Experiment 02 attempts: receipts, one transport retry, refusals that stop."""

import json
from pathlib import Path

import pytest

from nanolab.comparison.experiment_01 import (
    CELL_RESULT,
    DIAGNOSTICS,
    EVENTS,
    SUMMARY,
    Outcome,
    mark_load_started,
)
from nanolab.comparison.experiment_02 import (
    CONFIGURATIONS_BY_KEY,
    SAMPLES,
    SCHEDULER,
    Experiment02Cell,
)
from nanolab.config.experiment_02 import Budget
from nanolab.tasks.experiment_02.cell import CLEANUP_ERRORS

IDENTITY = {"tag": "recipe-test", "experiment": {"profile": "experiment-02"}}
CELL = Experiment02Cell(
    CONFIGURATIONS_BY_KEY["nanofaas-p1-jvm"], Budget(cpus=2, memoryMiB=2048), 1
)


def _measured(run_dir: Path) -> Outcome:
    (run_dir / "metrics").mkdir(parents=True, exist_ok=True)
    for name in (SUMMARY, EVENTS, SAMPLES, SCHEDULER):
        (run_dir / name).write_text("{}\n")
    return "measured"


def _startup_failed(run_dir: Path) -> Outcome:
    (run_dir / DIAGNOSTICS).mkdir(parents=True, exist_ok=True)
    (run_dir / DIAGNOSTICS / "error.txt").write_text("no correct response\n")
    return "startup_failed"


def _transport(run_dir: Path) -> Outcome:
    raise RuntimeError("channel closed")


def _refused(run_dir: Path) -> Outcome:
    raise ValueError("the measured slice enforces cpu1-mem2048")


def _after_load(run_dir: Path) -> Outcome:
    mark_load_started(run_dir)
    raise RuntimeError("k6 exited 107 without a summary")


def _run(tmp_path: Path, behaviours):
    from nanolab.cli.comparison import _run_experiment_02_cell

    calls: list[Path] = []
    checks: list[str] = []

    def run_attempt(run_dir: Path) -> Outcome:
        behaviour = behaviours[len(calls)]
        calls.append(run_dir)
        return behaviour(run_dir)

    outcome = _run_experiment_02_cell(
        CELL,
        root=tmp_path,
        identity=IDENTITY,
        before_attempt=lambda: checks.append("check"),
        run_attempt=run_attempt,
    )
    return outcome, calls, checks


def _receipt(tmp_path: Path) -> dict:
    return json.loads((CELL.run_dir(tmp_path) / CELL_RESULT).read_text())


def test_measured_attempt_commits_a_receipt_with_cgroup_samples(tmp_path):
    outcome, calls, checks = _run(tmp_path, [_measured])
    assert outcome == "measured"
    assert len(calls) == len(checks) == 1
    receipt = _receipt(tmp_path)
    assert receipt["outcome"] == "measured"
    assert SAMPLES in receipt["evidence"]


def test_startup_failure_is_a_result_not_an_error(tmp_path):
    outcome, calls, _checks = _run(tmp_path, [_startup_failed])
    assert outcome == "startup_failed"
    assert len(calls) == 1
    assert _receipt(tmp_path)["notProduced"] == [SUMMARY, EVENTS, SAMPLES, SCHEDULER]


def test_transport_failure_before_load_is_retried_once(tmp_path):
    outcome, calls, checks = _run(tmp_path, [_transport, _measured])
    assert outcome == "measured"
    assert [path.name for path in calls] == ["1", "2"]
    assert len(checks) == 2
    error = calls[0] / DIAGNOSTICS / "error.txt"
    assert "channel closed" in error.read_text()
    assert _receipt(tmp_path)["attempt"] == 2


def test_second_transport_failure_stops_the_matrix(tmp_path):
    with pytest.raises(RuntimeError, match="channel closed"):
        _run(tmp_path, [_transport, _transport])
    assert not (CELL.run_dir(tmp_path) / CELL_RESULT).exists()


def test_refusal_stops_without_retry(tmp_path):
    with pytest.raises(ValueError, match="cpu1-mem2048"):
        _run(tmp_path, [_refused, _measured])
    assert not (CELL.run_dir(tmp_path) / CELL_RESULT).exists()


def test_failure_after_the_load_started_is_not_retried(tmp_path):
    with pytest.raises(RuntimeError, match="k6"):
        _run(tmp_path, [_after_load, _measured])
    assert not (CELL.run_dir(tmp_path) / CELL_RESULT).exists()


def _cleanup_failed(outcome):
    def behaviour(run_dir: Path) -> Outcome:
        result = (_measured if outcome == "measured" else _startup_failed)(run_dir)
        (run_dir / DIAGNOSTICS).mkdir(parents=True, exist_ok=True)
        (run_dir / DIAGNOSTICS / CLEANUP_ERRORS).write_text('[{"step": "teardown"}]')
        return result

    return behaviour


def test_cleanup_failure_records_a_documented_failure_but_stops_the_matrix(tmp_path):
    with pytest.raises(RuntimeError, match="cleanup failed"):
        _run(tmp_path, [_cleanup_failed("startup_failed"), _measured])
    assert _receipt(tmp_path)["outcome"] == "startup_failed"


def test_cleanup_failure_never_keeps_a_measurement(tmp_path):
    with pytest.raises(RuntimeError, match="cleanup failed"):
        _run(tmp_path, [_cleanup_failed("measured"), _measured])
    assert not (CELL.run_dir(tmp_path) / CELL_RESULT).exists()


def test_transport_failure_with_a_failed_cleanup_is_not_retried(tmp_path):
    def failed(run_dir: Path) -> Outcome:
        (run_dir / DIAGNOSTICS).mkdir(parents=True, exist_ok=True)
        (run_dir / DIAGNOSTICS / CLEANUP_ERRORS).write_text("[]")
        raise RuntimeError("channel closed")

    with pytest.raises(RuntimeError, match="channel closed"):
        _run(tmp_path, [failed, _measured])
