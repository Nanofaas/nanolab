"""Saved experiment-02 data can be analysed without sources or infrastructure."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import typer
from typer.testing import CliRunner

from nanolab.cli import comparison
from nanolab.comparison.cells import EVENTS, SUMMARY, attempt_dir, write_cell_result
from nanolab.comparison.experiment_02 import SAMPLES, SCHEDULER, required_evidence
from nanolab.comparison.experiment_02_campaign import (
    archive_experiment_02_inputs,
    write_experiment_02_campaign_report,
)
from nanolab.comparison.manifest import (
    new_comparison_manifest,
    write_comparison_manifest,
)
from nanolab.config.experiment_02 import Experiment02Protocol
from tests.comparison.test_experiment_02 import SCENARIOS, SELECTED, _capture


def _saved_run(tmp_path, *, archive=True):
    captured, cells = _capture(tmp_path)
    inputs = cast(dict[str, Any], captured)
    protocol = Experiment02Protocol.model_validate(inputs["experiment"]["protocol"])
    payload = (SCENARIOS / "payloads" / protocol.payload_file).read_bytes()
    inputs["experiment"]["payload"].update(
        sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload)
    )
    inputs["experiment"]["configurations"]["nanofaas-p1-jvm"]["label"] = (
        "Saved JVM label"
    )
    root = tmp_path / "run"
    manifest = new_comparison_manifest(
        inputs, cells, variants=[{"key": key} for key in SELECTED]
    )
    write_comparison_manifest(root, manifest)
    cell = next(cell for cell in cells if cell.key == "nanofaas-p1-jvm")
    selected = attempt_dir(cell.run_dir(root), 1)
    (selected / "metrics").mkdir(parents=True)
    events = [{"t": "origin", "ts": 1_000_000}]
    for phase in protocol.phases():
        if phase["rate"]:
            sent = (1000 + phase["startSeconds"] + 1) * 1000
            events.extend(
                [
                    {
                        "t": "send",
                        "id": phase["name"],
                        "phase": phase["name"],
                        "ts": sent,
                    },
                    {
                        "t": "done",
                        "id": phase["name"],
                        "phase": phase["name"],
                        "ts": sent + 100,
                        "dur": 100,
                        "status": 200,
                        "ok": True,
                    },
                ]
            )
    (selected / EVENTS).write_text("\n".join(json.dumps(v) for v in events) + "\n")
    (selected / SUMMARY).write_text('{"metrics":{}}')
    (selected / SCHEDULER).write_text("")
    end = protocol.phases()[-1]
    duration = end["startSeconds"] + end["durationSeconds"]
    samples = [
        {
            "ts": at,
            "cgroups": {
                ".": {
                    "memoryCurrent": 1000,
                    "inactiveFile": 0,
                    "cpuUsageUsec": (at - 995) * 500000,
                }
            },
        }
        for at in range(995, 1000 + duration + 10, 5)
    ]
    (selected / SAMPLES).write_text("\n".join(json.dumps(v) for v in samples) + "\n")
    (selected / "readiness.json").write_text('{"firstCorrectSeconds":2.0}')
    write_cell_result(
        cell,
        root,
        identity=manifest.identity,
        attempt=1,
        outcome="measured",
        reason="fixture",
        started_at=datetime.now(UTC),
        required=required_evidence(cell.configuration),
    )
    if archive:
        archive_experiment_02_inputs(
            root, payload=payload, script=tmp_path / "experiment-02.js"
        )
    return root, selected


def _snapshot(root):
    return {
        str(p.relative_to(root)): (
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
        )
        for p in root.rglob("*")
        if p.is_file() and "analysis" not in p.parts
    }


def test_report_command_is_offline_and_preserves_raw_evidence(tmp_path, monkeypatch):
    root, selected = _saved_run(tmp_path)
    before = _snapshot(root)
    monkeypatch.delenv("NANOFAAS_ROOT", raising=False)
    monkeypatch.setattr(
        comparison,
        "provider_for_environment",
        lambda *a, **k: pytest.fail("offline report touched a provider"),
    )
    app = typer.Typer()
    comparison.register(app)
    result = CliRunner().invoke(app, ["compare-report", str(root)])
    assert result.exit_code == 0, (result.stdout, result.exception)
    report = json.loads((root / "analysis/experiment-02-campaign.json").read_text())
    assert report["serviceDefinition"] == "correct-response"
    assert report["measurement"]["identity"]["nanofaas"]["revision"]
    row = next(
        r for r in report["reference"] if r["configuration"] == "nanofaas-p1-jvm"
    )
    assert row["label"] == "Saved JVM label"
    assert row["metrics"]["correctThroughput"]["mean"] == pytest.approx(1 / 60)
    assert row["metrics"]["sentThroughput"]["mean"] == pytest.approx(1 / 60)
    assert row["metrics"]["droppedIterations"]["mean"] == 0
    missing = next(
        r for r in report["reference"] if r["configuration"] == "direct-reference"
    )
    assert missing["outcomes"]["incomplete"] == 1
    assert missing["metrics"]["correctThroughput"]["mean"] is None
    assert report["archivedInputs"]["payload"] == "inputs/experiment-02-payload.json"
    assert any(
        str((selected / EVENTS).relative_to(root)) in item["files"]
        for item in report["rawArtifacts"]
    )
    assert (root / "analysis/experiment-02-reference.csv").is_file()
    assert (root / "analysis/experiment-02-budget-grid.csv").is_file()
    assert before == _snapshot(root)


def test_changed_receipt_evidence_is_not_aggregated(tmp_path):
    root, selected = _saved_run(tmp_path)
    (selected / EVENTS).write_text("changed")
    with pytest.raises(ValueError, match="evidence missing or changed"):
        write_experiment_02_campaign_report(root)
    assert not (root / "analysis").exists()


def test_old_run_without_archived_inputs_is_still_analysable(tmp_path):
    root, _selected = _saved_run(tmp_path, archive=False)
    result = write_experiment_02_campaign_report(root)
    assert json.loads(result.read_text())["archivedInputs"] == {
        "payload": None,
        "generator": None,
    }


def test_archived_inputs_are_exact_and_never_replaced(tmp_path):
    script = tmp_path / "source.js"
    script.write_bytes(b"// immutable generator\n")
    root = tmp_path / "run"
    archive_experiment_02_inputs(root, payload=b'{"exact":true}\n', script=script)
    before = _snapshot(root)
    archive_experiment_02_inputs(root, payload=b'{"exact":true}\n', script=script)
    assert before == _snapshot(root)
    with pytest.raises(ValueError, match="archived input"):
        archive_experiment_02_inputs(root, payload=b"other", script=script)
    assert before == _snapshot(root)


def test_changed_archived_generator_is_rejected(tmp_path):
    root, _selected = _saved_run(tmp_path)
    (root / "inputs/experiment-02-generator.js").write_text("changed")
    with pytest.raises(ValueError, match="archived input differs"):
        write_experiment_02_campaign_report(root)
    assert not (root / "analysis").exists()
