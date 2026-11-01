"""The experiment preserves failures and stages the real corpus within argv limits."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.tasks.models import TaskResult

from nanolab.assets.diagnostics.idle_reactivation_probe import load_inputs
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.offload_experiment import RunOffloadExperiment
from tests.config.test_offload_experiment import scenario
from tests.conftest import source_contract_root

pytestmark = pytest.mark.nanofaas


def make_task(tmp_path, executor, fetcher):
    return RunOffloadExperiment(
        config=ScenarioConfig.model_validate(scenario()),
        source_root=source_contract_root(),
        run_dir=tmp_path,
        remote_home=tmp_path / "remote",
        edge_url="http://edge:30080",
        cloud_url="http://cloud:30080",
        executor=executor,
        fetcher=fetcher,
        namespace="nanofaas",
    )


def test_real_corpus_staging_is_bounded_and_lossless(tmp_path):
    seen = []

    def execute(spec):
        seen.append(spec)
        assert sum(len(arg.encode()) + 1 for arg in spec.argv) < 50000
        result = subprocess.run(spec.argv, capture_output=True, text=True, check=False)
        return TaskResult(
            task_id=spec.task_id,
            status="passed",
            return_code=result.returncode,
            stderr=result.stderr,
        )

    task = make_task(tmp_path, SimpleNamespace(run=execute), None)
    corpus = task.source_root / "functions/test-data/word-stats"
    fixtures = load_inputs(
        corpus / "performance-medium.json", corpus / "runtime-contract.json"
    )
    remote = tmp_path / "staged"
    task._stage_inputs(remote, fixtures)
    assert len(seen) > 1
    assert json.loads((remote / "inputs.json").read_text()) == fixtures
    with pytest.raises(RuntimeError):
        task._stage_inputs(remote, fixtures)


def test_k6_failure_collects_evidence_before_raising(tmp_path, monkeypatch):
    import nanolab.tasks.offload_experiment as module

    calls = []

    def execute(spec):
        calls.append(spec.task_id)
        stdout = json.dumps({"captured_at": 100, "errors": []})
        return TaskResult(
            task_id=spec.task_id, status="passed", return_code=0, stdout=stdout
        )

    def fetch(remote, local):
        calls.append(Path(remote).name)
        (local / Path(remote).name).write_text("{}")

    def fail_k6(self, inputs):
        raise RuntimeError("injected k6 crash")

    def analyze(path):
        assert (path / "k6-events.jsonl").exists()
        assert (path / "k6-summary.json").exists()
        assert (path / "observations/stack-after.json").exists()
        assert (path / "observations/cloud-after.json").exists()
        assert "logs" in calls
        evidence = json.loads((path / "execution.json").read_text())
        assert evidence["k6_passed"] is False
        assert "injected k6 crash" in evidence["errors"]
        calls.append("analysis")
        return {"integrity_passed": False}

    monkeypatch.setattr(module.K6Task, "run", fail_k6)
    monkeypatch.setattr(
        "nanolab.tasks.offload_experiment_report.analyze_experiment", analyze
    )
    task = make_task(
        tmp_path,
        SimpleNamespace(run=execute, binding_key=lambda role: role),
        SimpleNamespace(fetch_from=fetch),
    )
    task.logs = (SimpleNamespace(title="logs", run=lambda _: calls.append("logs")),)
    with pytest.raises(RuntimeError, match="injected k6 crash"):
        task.run(TaskInputs.empty())
    assert calls.index("k6-events.jsonl") < calls.index("analysis")


def test_unreachable_cloud_fails_before_load_and_retains_route(tmp_path):
    calls = []

    def execute(spec):
        calls.append(spec.task_id)
        failed = spec.task_id == "b4.cloud-route"
        return TaskResult(
            task_id=spec.task_id,
            status="failed" if failed else "passed",
            return_code=7 if failed else 0,
            stdout=json.dumps({"captured_at": 100, "errors": []}),
            stderr="connection refused" if failed else "",
        )

    def fetch(remote, local):
        raise FileNotFoundError(remote)

    task = make_task(
        tmp_path, SimpleNamespace(run=execute), SimpleNamespace(fetch_from=fetch)
    )
    with pytest.raises(RuntimeError, match="Edge cannot reach"):
        task.run(TaskInputs.empty())
    assert not any(name.startswith("b4.inputs") for name in calls)
    assert json.loads((tmp_path / "cloud-route.json").read_text())["return_code"] == 7
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "observations/cloud-after.json").exists()
