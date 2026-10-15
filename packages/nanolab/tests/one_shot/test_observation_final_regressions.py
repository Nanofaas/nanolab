"""Failure boundaries exercised by the final independent review."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from nanolab.config.one_shot import OperationalLimits
from nanolab.one_shot.client import NanoFaasOneShotClient
from nanolab.tasks.one_shot import experiment, qualification

pytestmark = pytest.mark.usefixtures("simulated_one_shot_host")


def test_collection_stops_before_retry_or_queued_proof_work(tmp_path, monkeypatch):
    from threading import Event

    stop = Event()
    calls = []
    run = SimpleNamespace(
        run_dir=tmp_path,
        topology=SimpleNamespace(
            resources=SimpleNamespace(nodes={"cloud": SimpleNamespace()}),
            function_settings={"work": SimpleNamespace(max_replicas=1)},
            endpoints={"cloud": "endpoint"},
        ),
    )
    monkeypatch.setattr(
        experiment, "runtime_endpoint", lambda *a, **k: ("http://sdk", {})
    )

    def respond(request):
        if request.url.path.startswith("/runtime/executions/"):
            calls.append(str(request.url))
            stop.set()
            raise httpx.ReadTimeout("proof unavailable")
        if request.url.path == "/runtime/status":
            return httpx.Response(200, json={"physicalReleaseProof": True})
        return httpx.Response(200, text="nanofaas_runtime_active_handlers 0\n")

    rows = [
        {
            "event": "result",
            "phase": "campaign",
            "originalId": str(i),
            "terminalExecutionId": str(i),
            "function": "work",
        }
        for i in range(256)
    ]
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(experiment.httpx, "Client", lambda **_: nullcontext(http))
        experiment.collect_runtime_evidence(
            run,
            cast(Any, SimpleNamespace(resource=lambda _: "http://gateway:8080")),
            rows,
            stop=stop,
        )
    assert len(calls) == 1


def test_failed_backend_copy_retains_sole_copy_volume(tmp_path, monkeypatch):
    from sonata_engine import TaskInputs

    from nanolab.tasks.one_shot.logging import backend_resource
    from tests.one_shot.test_logging import settings

    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "cp"]:
            raise OSError("archive destination full")
        return SimpleNamespace(
            returncode=0, stdout="owned-loki\n" if "ps" in argv else "", stderr=""
        )

    monkeypatch.setattr("nanolab.tasks.one_shot.logging.subprocess.run", command)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.logging.subprocess.check_output", lambda *a, **k: ""
    )
    logs = settings(
        {
            "enabled": True,
            "backendOwned": True,
            "backendBind": "10.0.0.1",
            "endpoint": "http://10.0.0.1:3100/loki/api/v1/push",
        }
    ).observability.logs
    resource = backend_resource(logs, root=tmp_path, run_dir=tmp_path)
    assert logs.endpoint is not None
    with pytest.raises(RuntimeError, match="archive"):
        resource.release(TaskInputs._for_resources({}, set()), logs.endpoint)
    assert any("down" in argv for argv in calls)
    assert not any("down" in argv and "-v" in argv for argv in calls)
    assert (tmp_path / "logging/backend-recovery.json").exists()


def test_entry_reporting_failure_keeps_original_error(tmp_path, monkeypatch):
    import importlib.util
    import json
    import sys

    from sonata_engine import Task, TaskOutcome, Workflow

    path = Path(__file__).parents[4] / "scripts/one-shot-observe.py"
    spec = importlib.util.spec_from_file_location("review_observer", path)
    assert spec is not None and spec.loader is not None
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    (freeze / "freeze.json").write_text(
        json.dumps(
            {
                "hostBootId": Path("/proc/sys/kernel/random/boot_id")
                .read_text()
                .strip(),
                "nanolabPackageSha256": observer.package_fingerprint(),
                "python": sys.version,
                "dependencies": {},
            }
        )
    )
    (freeze / "operator-protocol.json").write_text("{}")
    (freeze / "scenario.yaml").write_text("workflow: one-shot-calibration\n")
    monkeypatch.setattr(observer, "verify_files", lambda *a: None)
    monkeypatch.setattr(observer, "validate_operator_protocol", lambda *a: None)
    monkeypatch.setattr(observer.ScenarioConfig, "model_validate", lambda *a: object())
    monkeypatch.setattr(observer, "retain_offline_report", lambda *a: None)
    monkeypatch.setattr(
        observer.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout='{"list":[]}'),
    )
    original_save = observer.save

    def save(path, value):
        if path.name == "failure.json":
            raise OSError("report disk full")
        return original_save(path, value)

    monkeypatch.setattr(observer, "save", save)

    class Fail(Task[None]):
        title = "Fail operationally"

        def run(self, inputs) -> TaskOutcome[None]:
            raise RuntimeError("original process fault")

    workflow = Workflow("primary-cause")
    workflow.add(Fail())
    monkeypatch.setattr(
        observer, "build_one_shot_observation_plan", lambda *a, **k: workflow
    )
    with pytest.raises(RuntimeError, match="original process fault"):
        observer.run_observation(
            SimpleNamespace(
                freeze=freeze,
                output=tmp_path / "run",
                archive=None,
            )
        )
    result = json.loads((tmp_path / "run/result.json").read_text())
    assert "original process fault" in result["cause"]


def test_sonata_retains_only_vm_containing_unexported_raw(tmp_path, monkeypatch):
    from sonata_engine import Task, TaskOutcome, Workflow

    from nanolab.config import EnvironmentConfig
    from nanolab.one_shot.infrastructure import build_one_shot_resources
    from tests.one_shot.test_resources import Provider, config

    provider = Provider()
    commands = []
    monkeypatch.setattr(
        "nanolab.one_shot.infrastructure.provider_for", lambda *a: provider
    )
    monkeypatch.setattr(
        "nanolab.one_shot.infrastructure.subprocess.run",
        lambda argv, **kw: commands.append(argv),
    )
    resources = build_one_shot_resources(
        config(),
        EnvironmentConfig.model_validate(
            {
                "provider": "multipass",
                "roles": {"stack": {"name": "review"}},
            }
        ),
        run_id="recovery",
        repo_root=tmp_path,
    )
    retained = resources.nodes["edge-0"]
    other = resources.nodes["edge-1"]

    class FailExport(Task[None]):
        title = "Preserve unexported collector state"

        def run(self, inputs) -> TaskOutcome[None]:
            retained.recovery.append("collector volume still contains raw")
            raise RuntimeError("raw export failed")

    workflow = Workflow("retain-sole-copy")
    workflow.add(FailExport(), requires=(retained.vm, other.vm))
    with pytest.raises(RuntimeError, match="raw export failed"):
        workflow.run()
    assert provider.released == [other.request.name]
    assert commands == [("multipass", "stop", retained.request.name)]


def test_slow_diagnostics_cannot_consume_next_epoch_lead(tmp_path, monkeypatch):
    now = [100.0]
    diagnostic_reads = []
    monkeypatch.setattr(
        qualification,
        "time",
        SimpleNamespace(
            monotonic=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        ),
    )

    def respond(request):
        if request.url.path.endswith(("events", "prometheus")):
            diagnostic_reads.append(request.url.path)
            now[0] += request.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("diagnostic endpoint unavailable")
        preparing = request.url.path.endswith("prepare")
        return httpx.Response(
            200,
            json={
                "status": "NEGOTIATED" if preparing else "DEGRADED",
                "plan": None if preparing else {"functions": {"f": {}}},
                "desiredReplicas": {"f": 1},
                "readyReplicas": {"f": 1},
                "reason": "ACK response missing",
                "preparedAt": "2026-10-13T00:00:00Z",
            },
        )

    start = datetime(2026, 10, 13, tzinfo=UTC)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        clients = {
            node: NanoFaasOneShotClient(
                "http://" + node,
                http=http,
                timeout_seconds=180,
            )
            for node in ("a", "b")
        }
        for epoch in range(2):
            sample, _ = qualification.prepare_parallel_epoch(
                clients,
                epoch=epoch,
                starts_at=start + timedelta(seconds=360 * epoch),
                ends_at=start + timedelta(seconds=360 * (epoch + 1)),
                output=tmp_path / "epochs.jsonl",
                session_id="s",
                activation_at=190 + epoch * 360,
                clock=cast(Any, SimpleNamespace(require_healthy=lambda: None)),
                observation_only=True,
                settings=SimpleNamespace(operational_limits=OperationalLimits()),
            )
            assert not sample.censored
    assert not diagnostic_reads  # The existing collector owns diagnostic I/O.
    assert now[0] == 550


@pytest.mark.parametrize(
    "target", ["metrics.jsonl", "cloud-work-1-metrics.txt", "physical.jsonl"]
)
def test_physical_collector_propagates_artifact_write_failure(
    tmp_path,
    monkeypatch,
    target,
):
    run = SimpleNamespace(
        run_dir=tmp_path,
        topology=SimpleNamespace(
            resources=SimpleNamespace(nodes={"cloud": SimpleNamespace()}),
            function_settings={"work": SimpleNamespace(max_replicas=1)},
            endpoints={"cloud": "endpoint"},
        ),
    )
    row = {
        "event": "result",
        "phase": "campaign",
        "originalId": "id",
        "executionId": "id",
        "terminalExecutionId": "id",
        "function": "work",
    }
    original_open = Path.open

    def open_file(path, mode="r", *args, **kwargs):
        if path.name == target and any(flag in mode for flag in "wax"):
            raise OSError("evidence disk full")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(
        experiment, "runtime_endpoint", lambda *a, **k: ("http://sdk", {})
    )

    def respond(request):
        if request.url.path == "/runtime/status":
            return httpx.Response(200, json={"physicalReleaseProof": True})
        if request.url.path.endswith("/runtime/executions/id"):
            return httpx.Response(
                200,
                json={
                    "executionId": "id",
                    "incarnation": "replica",
                    "dispatchAttempt": "1",
                    "state": "RELEASED",
                    "handlerStarted": True,
                },
            )
        return httpx.Response(200, text="nanofaas_runtime_active_handlers 0\n")

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(experiment.httpx, "Client", lambda **_: nullcontext(http))
        with pytest.raises(OSError, match="evidence disk full"):
            experiment.collect_runtime_evidence(
                run,
                cast(Any, SimpleNamespace(resource=lambda _: "http://gateway:8080")),
                [row],
            )
