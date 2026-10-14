"""Backend health must prove ingestion, not just a running process or /ready."""

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sonata_engine import TaskInputs

from nanolab.tasks.one_shot.logging import backend_resource
from tests.one_shot.test_logging import settings


def fake_backend(
    tmp_path,
    monkeypatch,
    *,
    push_status=204,
    visible=True,
    supervision=None,
    wal_metric="0",
):
    calls = []
    writes = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "cp"]:
            path = Path(argv[-1])
            path.mkdir(parents=True, exist_ok=True)
            (path / "wal").write_text("retained")
        if "--format" in argv:
            stdout = json.dumps({"State": "running"})
        elif "ps" in argv:
            stdout = "owned-loki\n"
        else:
            stdout = ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def respond(request):
        if request.method == "POST":
            row = json.loads(request.content)
            writes.append(row)
            status = push_status() if callable(push_status) else push_status
            return httpx.Response(
                status,
                text="" if status == 204 else "Ingester is shutting down",
            )
        if request.url.path.endswith("/query_range"):
            values = writes[-1]["streams"][0]["values"] if visible and writes else []
            return httpx.Response(
                200,
                json={"status": "success", "data": {"result": [{"values": values}]}},
            )
        if request.url.path == "/metrics":
            return httpx.Response(
                200,
                text=("loki_ingester_wal_disk_full_failures_total " + wal_metric + "\n")
                if wal_metric is not None
                else "",
            )
        return httpx.Response(200, text="ready")

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.logging.httpx.Client", lambda **_: nullcontext(client)
    )
    monkeypatch.setattr("nanolab.tasks.one_shot.logging.subprocess.run", command)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.logging.subprocess.check_output", lambda *_, **__: ""
    )
    config = settings(
        {
            "enabled": True,
            "endpoint": "http://10.0.0.1:3100/loki/api/v1/push",
            "backendOwned": True,
            "backendBind": "10.0.0.1",
        }
    ).observability.logs
    resource = backend_resource(
        config, root=tmp_path, run_dir=tmp_path, supervision=supervision
    )
    return resource, TaskInputs._for_resources({}, set()), calls, writes


@pytest.mark.parametrize("fault", ["push", "invisible"])
def test_ready_but_unwritable_or_unreadable_backend_stops_and_compensates(
    tmp_path, monkeypatch, fault
):
    resource, inputs, calls, writes = fake_backend(
        tmp_path,
        monkeypatch,
        push_status=500 if fault == "push" else 204,
        visible=fault != "invisible",
    )
    with pytest.raises(RuntimeError, match=r"Loki.*(ingestion|probe)"):
        resource.acquire(inputs)
    assert len(writes) == 1  # no automatic write retry after readiness succeeded
    assert any("down" in row for row in calls)
    assert (tmp_path / "logging/backend-data/wal").read_text() == "retained"
    assert (tmp_path / "logging/backend-metrics-on-failure.txt").exists()
    journal = [
        json.loads(line)
        for line in (tmp_path / "logging/backend-probes.jsonl").read_text().splitlines()
    ]
    assert journal[-1]["state"] == "failed"


def test_owned_backend_explicitly_keeps_wal_without_shared_disk_percentage_gate(
    tmp_path, monkeypatch
):
    resource, inputs, calls, writes = fake_backend(tmp_path, monkeypatch)
    value = resource.acquire(inputs)
    resource.release(inputs, value)
    override = tmp_path / "logging/backend-compose.override.json"
    command = json.loads(override.read_text())["services"]["loki"]["command"]
    assert "-ingester.wal-disk-full-threshold=0" in command
    assert not any("wal-enabled=false" in arg for arg in command)
    assert any(str(override) in argv for argv in calls)
    assert len(writes) == 1


def test_running_backend_with_ingestion_fault_is_detected_by_service_probe(
    tmp_path, monkeypatch
):
    probes = []

    class Supervision:
        def service_resource(self, resource, name, probe, **kwargs):
            assert name == "logging-backend"
            probes.append(probe)
            return resource

    status = [204]
    resource, inputs, _, writes = fake_backend(
        tmp_path, monkeypatch, push_status=lambda: status[0], supervision=Supervision()
    )
    value = resource.acquire(inputs)
    status[0] = 500
    assert probes
    with pytest.raises(RuntimeError, match=r"Loki.*(ingestion|probe)"):
        probes[0](value)
    resource.release(inputs, value)
    assert len(writes) == 2


def test_sonata_ingestion_fault_archives_and_cleans_without_restart(
    tmp_path, monkeypatch
):
    import time

    from sonata_engine import Task, TaskOutcome, Workflow

    from nanolab.one_shot.supervision import CampaignSupervision

    archive = tmp_path / "archive"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    supervisor = CampaignSupervision(run_dir, archive=archive, cadence=0.01)
    original = supervisor.service_resource

    def fast_service(resource, name, probe, **kwargs):
        return original(resource, name, probe, interval_seconds=0.01, **kwargs)

    monkeypatch.setattr(supervisor, "service_resource", fast_service)
    successes = [0]

    def status():
        successes[0] += 1
        return 204 if successes[0] < 3 else 500

    resource, _, calls, _ = fake_backend(
        run_dir, monkeypatch, push_status=status, supervision=supervisor
    )

    class Wait(Task):
        title = "Observe owned Loki ingestion"

        def run(self, inputs):
            inputs.resource(resource)
            time.sleep(0.2)
            return TaskOutcome()

    workflow = Workflow("loki-ingestion-failure")
    workflow.add(Wait(), requires=(resource,))
    with pytest.raises(RuntimeError, match="logging-backend"):
        supervisor.run(workflow.run)
    result = json.loads((run_dir / "result.json").read_text())
    assert result["state"] == "failed" and result["exitCode"] == 1
    assert result["archiveVerified"]
    assert (archive / "logging/backend-data/wal").read_text() == "retained"
    assert any("down" in argv for argv in calls)
    assert sum("up" in argv for argv in calls) == 1
    assert not supervisor.checks


def test_override_write_failure_creates_no_backend_resources(tmp_path, monkeypatch):
    resource, inputs, calls, _ = fake_backend(tmp_path, monkeypatch)

    def disk_full(*args, **kwargs):
        raise OSError("configuration disk full")

    monkeypatch.setattr(Path, "write_text", disk_full)
    with pytest.raises(OSError, match="configuration disk full"):
        resource.acquire(inputs)
    assert calls == []


@pytest.mark.parametrize("wal_metric", ["1", "nan", None])
def test_queryable_memory_only_or_unknown_wal_is_not_healthy(
    tmp_path, monkeypatch, wal_metric
):
    resource, inputs, calls, writes = fake_backend(
        tmp_path, monkeypatch, wal_metric=wal_metric
    )
    with pytest.raises(RuntimeError, match=r"Loki.*WAL"):
        resource.acquire(inputs)
    assert len(writes) == 1
    assert any("down" in argv for argv in calls)
    assert (tmp_path / "logging/backend-data/wal").read_text() == "retained"
