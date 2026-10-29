"""Experiment 02 attempts: receipts, one transport retry, refusals that stop."""

import json
from pathlib import Path

import pytest

from nanolab.comparison.cells import (
    CELL_RESULT,
    DIAGNOSTICS,
    EVENTS,
    SUMMARY,
    Outcome,
    attempt_dir,
    mark_load_started,
)
from nanolab.comparison.experiment_02 import (
    CONFIGURATIONS_BY_KEY,
    SAMPLES,
    SCHEDULER,
    Experiment02Cell,
    build_experiment_02_matrix,
)
from nanolab.config.experiment_02 import Budget
from nanolab.tasks.experiment_02.cell import CLEANUP_ERRORS
from nanolab.tasks.experiment_02.refusal import CellRefusedError

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
    raise CellRefusedError("the measured slice enforces cpu1-mem2048")


def _garbled(run_dir: Path) -> Outcome:
    # Output cut short by a dropped channel: a ValueError, but no refusal.
    return json.loads('{"started": 1.')


def _after_load(run_dir: Path) -> Outcome:
    mark_load_started(run_dir)
    raise RuntimeError("k6 exited 107 without a summary")


def _run(tmp_path: Path, behaviours):
    from nanolab.application.experiment_02 import run_experiment_02_cell

    calls: list[Path] = []
    checks: list[str] = []

    def run_attempt(run_dir: Path) -> Outcome:
        behaviour = behaviours[len(calls)]
        calls.append(run_dir)
        return behaviour(run_dir)

    outcome = run_experiment_02_cell(
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
    with pytest.raises(CellRefusedError, match="cpu1-mem2048"):
        _run(tmp_path, [_refused, _measured])
    assert not (CELL.run_dir(tmp_path) / CELL_RESULT).exists()
    error = attempt_dir(CELL.run_dir(tmp_path), 1) / DIAGNOSTICS / "error.txt"
    assert "cpu1-mem2048" in error.read_text()


def test_unparseable_output_before_load_is_retried_not_refused(tmp_path):
    outcome, calls, _checks = _run(tmp_path, [_garbled, _measured])
    assert outcome == "measured"
    assert [path.name for path in calls] == ["1", "2"]
    assert "JSONDecodeError" in (calls[0] / DIAGNOSTICS / "error.txt").read_text()


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


def test_cleanup_failure_keeps_measurement_but_stops_the_matrix(tmp_path):
    with pytest.raises(RuntimeError, match="cleanup failed"):
        _run(tmp_path, [_cleanup_failed("measured"), _measured])
    assert _receipt(tmp_path)["outcome"] == "measured"
    assert "cleanup failed" in _receipt(tmp_path)["reason"]


def test_transport_failure_with_a_failed_cleanup_is_not_retried(tmp_path):
    def failed(run_dir: Path) -> Outcome:
        (run_dir / DIAGNOSTICS).mkdir(parents=True, exist_ok=True)
        (run_dir / DIAGNOSTICS / CLEANUP_ERRORS).write_text("[]")
        raise RuntimeError("channel closed")

    with pytest.raises(RuntimeError, match="channel closed"):
        _run(tmp_path, [failed, _measured])


@pytest.mark.parametrize("configuration", CONFIGURATIONS_BY_KEY.values())
@pytest.mark.parametrize(
    "budget", [Budget(cpus=2, memoryMiB=2048), Budget(cpus=4, memoryMiB=4096)]
)
@pytest.mark.parametrize("repetition", [1, 2, 3])
def test_resume_never_repeats_a_loaded_attempt(
    tmp_path, configuration, budget, repetition
):
    from nanolab.application.experiment_02 import run_experiment_02_cell

    cell = Experiment02Cell(configuration, budget, repetition)
    previous = attempt_dir(cell.run_dir(tmp_path), 1)
    previous.mkdir(parents=True)
    mark_load_started(previous)
    with pytest.raises(RuntimeError, match="load already started"):
        run_experiment_02_cell(
            cell,
            root=tmp_path,
            identity=IDENTITY,
            before_attempt=lambda: pytest.fail("must refuse before contacting VM"),
            run_attempt=lambda _: pytest.fail("must not repeat load"),
        )
    assert not attempt_dir(cell.run_dir(tmp_path), 2).exists()


def test_resume_refuses_unresolved_cleanup_even_with_a_receipt(tmp_path):
    with pytest.raises(RuntimeError, match="cleanup failed"):
        _run(tmp_path, [_cleanup_failed("startup_failed")])
    with pytest.raises(RuntimeError, match="unresolved cleanup"):
        _run(tmp_path, [_measured])


@pytest.mark.parametrize("configuration", CONFIGURATIONS_BY_KEY.values())
@pytest.mark.parametrize("outcome", ["measured", "startup_failed", "oom"])
def test_all_configurations_keep_evidence_on_permanent_cleanup_failure(
    tmp_path, configuration, outcome
):
    from nanolab.application.experiment_02 import run_experiment_02_cell
    from nanolab.comparison.cells import read_cell_result

    cell = Experiment02Cell(configuration, Budget(cpus=2, memoryMiB=2048), 1)
    calls = []

    def attempt(path):
        calls.append(path)
        _cleanup_failed("measured" if outcome == "measured" else "startup_failed")(path)
        return outcome

    with pytest.raises(RuntimeError, match="cleanup failed"):
        run_experiment_02_cell(
            cell,
            root=tmp_path,
            identity=IDENTITY,
            before_attempt=lambda: None,
            run_attempt=attempt,
        )
    receipt = read_cell_result(cell, tmp_path, IDENTITY)
    assert receipt is not None
    assert receipt["outcome"] == outcome
    assert "diagnostics/cleanup-errors.json" in receipt["evidence"]
    assert len(calls) == 1


@pytest.mark.parametrize("failed_key", CONFIGURATIONS_BY_KEY)
def test_teardown_failure_only_blocks_cells_sharing_the_target(tmp_path, failed_key):
    from nanolab.application.experiment_02 import (
        CellCleanupError,
        run_available_cells,
    )

    first = Experiment02Cell(CONFIGURATIONS_BY_KEY[failed_key], CELL.budget, 1)
    matrix = build_experiment_02_matrix(
        tuple(CONFIGURATIONS_BY_KEY.values()),
        (Budget(cpus=2, memoryMiB=2048), Budget(cpus=4, memoryMiB=4096)),
        3,
        seed=1,
    )
    assert len(matrix) == 66
    others = [cell for cell in matrix if cell != first]
    visited = []

    def run(cell):
        visited.append(cell)
        if cell == first:
            raise CellCleanupError("cleanup failed", target_only=True)

    blocked = run_available_cells([first, *others], run, notify=lambda _: None)
    target = "faasd" if failed_key == "faasd" else "stack"
    assert blocked == {target}
    assert visited == [
        first,
        *[cell for cell in others if (cell.key == "faasd") != (failed_key == "faasd")],
    ]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("generator failed"),
        CellRefusedError("target changed"),
    ],
)
def test_matrix_never_continues_after_shared_or_identity_failure(error):
    from nanolab.application.experiment_02 import run_available_cells

    calls = []

    def run(cell):
        calls.append(cell)
        raise error

    with pytest.raises(type(error), match=str(error)):
        run_available_cells([CELL, CELL], run, notify=lambda _: None)
    assert len(calls) == 1


def test_matrix_never_quarantines_missing_evidence_as_target_only():
    from nanolab.application.experiment_02 import CellCleanupError, run_available_cells

    def run(cell):
        raise CellCleanupError("fetch failed", target_only=False)

    with pytest.raises(CellCleanupError, match="fetch failed"):
        run_available_cells([CELL, CELL], run, notify=lambda _: None)


@pytest.mark.parametrize("loaded", [False, True])
def test_oom_with_cleanup_failure_cannot_quarantine_only_the_target(tmp_path, loaded):
    from nanolab.application.experiment_02 import (
        CellCleanupError,
        run_experiment_02_cell,
    )

    def run(path):
        if loaded:
            mark_load_started(path)
        _cleanup_failed("startup_failed")(path)
        return "oom"

    with pytest.raises(CellCleanupError) as caught:
        run_experiment_02_cell(
            CELL,
            root=tmp_path,
            identity=IDENTITY,
            before_attempt=lambda: None,
            run_attempt=run,
        )
    assert not caught.value.target_only


@pytest.mark.parametrize("tampered", [False, True])
def test_resume_validates_archived_receipts_for_earlier_loaded_attempts(
    tmp_path, tampered
):
    from nanolab.application.experiment_02 import require_safe_resume

    def measured(path):
        mark_load_started(path)
        return _measured(path)

    _run(tmp_path, [measured])
    _run(tmp_path, [measured])  # Explicit replacement archives the first receipt.
    first = attempt_dir(CELL.run_dir(tmp_path), 1)
    if tampered:
        (first / SUMMARY).write_text("changed")
        with pytest.raises(ValueError, match="evidence missing or changed"):
            require_safe_resume(CELL, tmp_path, IDENTITY)
    else:
        require_safe_resume(CELL, tmp_path, IDENTITY)
