"""The reference comparison judges per-edge work from nanoFaaS counters."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

PATH = Path(__file__).parents[4] / "scripts/one-shot-compare.py"
SPEC = importlib.util.spec_from_file_location("one_shot_compare", PATH)
assert SPEC is not None and SPEC.loader is not None
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)

START = 1_800_000_000.0


def report(*, completed, success, routing):
    return {
        "planned": 100,
        "emitted": 100,
        "generator_deficit": 0,
        "coverageSatisfied": True,
        "completed": completed,
        "routing": routing,
        "edgeOutcomesByNode": {"edge-1": {"completed": completed}},
        "originDestination": {"edge-1": {"edge-1": completed}},
        "returnedResponses": {
            "phases": {
                "campaign": {
                    "total": {
                        "successResponse": success,
                        "reject": 100 - success,
                        "httpError": 0,
                        "emitted": 100,
                    }
                }
            }
        },
        "latencySeconds": {"p50": 1.0, "p95": 2.0},
        "cloudDirect": {"planned": 10, "completed": 10, "errors": 0},
        "throughputOriginalsPerSecond": 1.0,
        "censored": 0,
        "auctionEpochs": [{"readySeconds": 30.0}],
    }


def sample(node, at, executions, handled):
    def counter(name, value):
        return [
            {"labels": {"function": function}, "name": name, "value": value / 2}
            for function in ("a", "b")
        ]

    return json.dumps(
        {
            "atUtc": datetime.fromtimestamp(at, UTC).isoformat(),
            "node": node,
            "samples": [
                *counter("function_latency_ms_seconds_count", executions),
                *counter("function_e2e_latency_ms_seconds_count", handled),
                *counter("function_latency_ms_seconds_bucket", 10**6),
                *counter("sync_queue_rejected_total", 0),
            ],
        }
    )


def run_dir(path, *, completed, executions, samples=True):
    path.mkdir()
    (path / "report.json").write_text(
        json.dumps(
            report(completed=completed, success=90, routing={"local": completed})
        )
    )
    (path / "result.json").write_text(json.dumps({"archiveVerified": True}))
    (path / "schedule.json").write_text(
        json.dumps(
            [
                {"phase": "warmup", "scheduledAt": START - 500},
                {"phase": "campaign", "scheduledAt": START},
                {"phase": "campaign", "scheduledAt": START + 100},
            ]
        )
    )
    if samples:
        # Warm-up work before the campaign, and work after the drain, must not
        # count: only the campaign's difference does.
        (path / "queue-samples-edge-1.jsonl").write_text(
            "\n".join(
                sample("edge-1", at, done, done * 2)
                for at, done in (
                    (START - 300, 40),
                    (START - 1, 50),
                    (START + 50, 80),
                    (START + 100 + compare.DRAIN_SECONDS, 50 + executions),
                    (START + 400, 10_000),
                )
            )
            + "\n"
        )


def document(tmp_path, reference, run):
    output = tmp_path / "out"
    compare.main([str(reference), str(run), "--output", str(output)])
    return json.loads((output / "comparison.json").read_text()), (
        output / "comparison.md"
    ).read_text()


def test_counters_judge_edge_work_and_low_proof_coverage_is_only_recorded(tmp_path):
    reference, run = tmp_path / "reference", tmp_path / "run"
    run_dir(reference, completed=80, executions=100)
    run_dir(run, completed=40, executions=105)
    result, text = document(tmp_path, reference, run)
    rows = {item["quantity"]: item for item in result["counters"]["mustMatch"]}
    assert rows["edge-1 executions"]["reference"] == 100
    assert rows["edge-1 executions"]["run"] == 105
    assert rows["edge-1 handled"]["run"] == 210
    assert all(item["ok"] for item in rows.values())
    # 40 of 90 successes proven: the halved proof-based counts do not decide.
    assert result["proofCoverage"]["judged"] is False
    assert not next(
        item for item in result["proofBased"] if item["quantity"] == "edge-1 completed"
    )["ok"]
    assert result["outsideTolerance"] == []
    assert "below 95%, recorded only" in text


def test_counter_drift_beyond_tolerance_is_reported(tmp_path):
    reference, run = tmp_path / "reference", tmp_path / "run"
    run_dir(reference, completed=90, executions=100)
    run_dir(run, completed=90, executions=150)
    result, _text = document(tmp_path, reference, run)
    assert result["proofCoverage"]["judged"] is True
    assert result["outsideTolerance"] == ["edge-1 executions", "edge-1 handled"]


def test_samples_may_live_apart_and_are_required_for_both_runs(tmp_path):
    reference, run = tmp_path / "reference", tmp_path / "run"
    run_dir(reference, completed=80, executions=100, samples=False)
    run_dir(run, completed=80, executions=100)
    result, text = document(tmp_path, reference, run)
    assert result["counters"] is None
    assert "Per-node counters not compared" in text
    run_dir(tmp_path / "samples", completed=80, executions=100)
    output = tmp_path / "apart"
    compare.main(
        [
            str(reference),
            str(run),
            "--reference-samples",
            str(tmp_path / "samples"),
            "--output",
            str(output),
        ]
    )
    assert json.loads((output / "comparison.json").read_text())["counters"]


def test_a_counter_reset_is_refused(tmp_path):
    run_dir(tmp_path / "run", completed=80, executions=-60)
    with pytest.raises(SystemExit, match="went back"):
        compare.node_counters(tmp_path / "run")
