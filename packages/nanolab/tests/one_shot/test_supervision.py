"""Exercise supervision through Sonata ownership, never through real VMs."""

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, cast

import pytest
from sonata_engine import Resource, Task, TaskOutcome, Workflow

from nanolab.one_shot.supervision import CampaignSupervision
from nanolab.one_shot.views import JsonCampaignPresentation


def json_supervision(*args, **kwargs):
    """Keep legacy stdout regressions explicit while programmatic defaults are quiet."""
    return CampaignSupervision(*args, presentation=JsonCampaignPresentation(), **kwargs)


class Action(Task):
    title = "Simulated campaign phase"

    def __init__(self, action):
        self.action = action

    def run(self, inputs):
        self.action(inputs)
        return TaskOutcome()


def test_success_dependencies_progress_and_archive(tmp_path, capsys):
    output, archive = tmp_path / "raw", tmp_path / "archive"
    output.mkdir()
    supervisor = json_supervision(output, archive=archive, cadence=0.01)
    releases = []
    base = Resource(
        title="Owned simulated VM",
        acquire=lambda _inputs: "vm",
        release=lambda _inputs, value: releases.append(value),
    )
    worker = supervisor.resource(
        Resource(
            title="Owned collector",
            acquire=lambda inputs: inputs.resource(base),
            release=lambda _inputs, value: releases.append("collector:" + value),
            requires=(base,),
        ),
        "collector",
        lambda _value: None,
    )
    workflow = Workflow("simulated")

    def action(inputs):
        assert inputs.resource(worker) == "vm"
        time.sleep(0.04)
        (output / "raw.jsonl").write_text('{"statusCode":429}\n')

    workflow.add(Action(action), requires=(base, worker))
    supervisor.run(workflow.run)
    assert releases == ["collector:vm", "vm"]
    status = json.loads((output / "status.json").read_text())
    assert status["state"] == "completed"
    assert status["exitCode"] == 0
    assert status["archiveVerified"] is True
    events = [
        json.loads(line)
        for line in (output / "supervision.jsonl").read_text().splitlines()
    ]
    assert any(
        row["kind"] == "task.started" and row["title"] == base.title for row in events
    )
    assert any(
        row["kind"] == "heartbeat" and row["activity"] == "waiting" for row in events
    )
    assert "completed" in capsys.readouterr().out
    assert not supervisor.thread.is_alive()
    for path in output.rglob("*"):
        if path.is_file():
            assert (
                path.read_bytes() == (archive / path.relative_to(output)).read_bytes()
            )


def test_process_failure_interrupts_wait_and_cleans_owned_resources(tmp_path):
    supervisor = json_supervision(
        tmp_path, archive=tmp_path.parent / "failed-archive", cadence=0.01
    )
    released = []

    def release(_inputs, child):
        child.wait(timeout=2)
        released.append(True)

    child_resource = supervisor.resource(
        Resource(
            title="Owned short-lived generator",
            acquire=lambda _inputs: subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(.05); raise SystemExit(7)",
                ]
            ),
            release=release,
        ),
        "generator",
        lambda child: supervisor.check_process(child, expected_end=float("inf")),
    )
    workflow = Workflow("failed-process")
    workflow.add(Action(lambda _inputs: time.sleep(2)), requires=(child_resource,))
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"generator.*exit 7"):
        supervisor.run(workflow.run)
    assert time.monotonic() - started < 1
    assert released == [True]
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["exitCode"] == 1
    assert result["state"] == "failed"
    assert supervisor.archive is not None
    assert (supervisor.archive / "supervision.jsonl").exists()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_interruption_preserves_raw_and_cleanup(tmp_path, signum):
    supervisor = json_supervision(tmp_path, cadence=0.01)
    released = []
    previous = signal.getsignal(signum)
    resource = Resource(
        title="Owned VM",
        acquire=lambda _: object(),
        release=lambda _, value: released.append(value),
    )
    workflow = Workflow("interrupted")

    def interrupt(_inputs):
        (tmp_path / "partial.jsonl").write_text('{"partial":true}\n')
        os.kill(os.getpid(), signum)

    workflow.add(Action(interrupt), requires=(resource,))
    with pytest.raises(RuntimeError, match="interrupted"):
        supervisor.run(workflow.run)
    assert len(released) == 1
    assert (tmp_path / "partial.jsonl").exists()
    assert (
        json.loads((tmp_path / "result.json").read_text())["exitCode"] == 128 + signum
    )
    assert signal.getsignal(signum) == previous


def test_first_fault_preserved_when_cleanup_fails(tmp_path):
    supervisor = json_supervision(tmp_path, cadence=0.01)

    def release(_inputs, _value):
        raise RuntimeError("cleanup failed")

    def fail(_inputs):
        raise RuntimeError("initial operational fault")

    resource = Resource(title="Owned VM", acquire=lambda _: True, release=release)
    workflow = Workflow("failed-cleanup")
    workflow.add(Action(fail), requires=(resource,))
    with pytest.raises(RuntimeError, match="initial operational fault"):
        supervisor.run(workflow.run)
    result = json.loads((tmp_path / "result.json").read_text())
    assert "initial operational fault" in result["cause"]
    assert any("cleanup failed" in row["detail"] for row in result["anomalies"])


def test_dependency_is_not_inferred_by_supervision(tmp_path):
    supervisor = json_supervision(tmp_path, cadence=0.01)
    base = Resource(title="Sampler", acquire=lambda _: True, release=lambda *_: None)
    child = supervisor.resource(
        Resource(
            title="Generator",
            acquire=lambda inputs: inputs.resource(base),
            release=lambda *_: None,
        ),
        "generator",
        lambda _: None,
    )
    workflow = Workflow("undeclared")
    workflow.add(Action(lambda _: None), requires=(base, child))
    with pytest.raises(Exception, match=r"not declared|undeclared|declared"):
        supervisor.run(workflow.run)
    assert json.loads((tmp_path / "result.json").read_text())["state"] == "failed"


def test_sampler_loss_is_observed_but_required_service_exit_is_fault(tmp_path):
    from types import SimpleNamespace

    from nanolab.one_shot.supervision import check_sampler

    path = tmp_path / "node.jsonl"
    session = SimpleNamespace(
        require_healthy=lambda: None,
        sessions=[
            {
                "output": SimpleNamespace(name=str(path)),
                "node": SimpleNamespace(config=SimpleNamespace(id="edge")),
            }
        ],
    )
    path.write_text(
        '{"errors":[],"containers":[{"role":"control-plane"},{"role":"collector"}]}\n'
    )
    check_sampler(session)
    path.write_text('{"partial":')
    check_sampler(session)
    path.write_text('{"errors":[{"error":"sample missing"}],"containers":[]}\n')
    check_sampler(session)
    path.write_text('{"errors":[],"containers":[{"role":"control-plane"}]}\n')
    with pytest.raises(RuntimeError, match=r"edge.*collector"):
        check_sampler(session)


def test_archive_failure_retains_raw_and_returns_failure(tmp_path):
    archive = tmp_path / "already-existing"
    archive.mkdir()
    output = tmp_path / "raw"
    output.mkdir()
    supervisor = json_supervision(output, archive=archive, cadence=0.01)
    with pytest.raises(FileExistsError):
        supervisor.run(lambda: (output / "partial.txt").write_text("retained"))
    result = json.loads((output / "result.json").read_text())
    assert result["exitCode"] == 1
    assert result["archiveVerified"] is False
    assert (output / "partial.txt").read_text() == "retained"


def test_missing_samples_are_not_an_operational_failure(tmp_path):
    from types import SimpleNamespace

    from nanolab.one_shot.vm_resources import VmTelemetrySession

    path = tmp_path / "node.jsonl"
    path.write_text('{"timestampUtc":"2026-10-12T00:00:00Z"}\n')
    sampler = object.__new__(VmTelemetrySession)
    sampler.run = SimpleNamespace(supervision=object())
    sampler.sessions = [
        {
            "process": SimpleNamespace(poll=lambda: None),
            "output": SimpleNamespace(name=str(path)),
            "readerErrors": [],
            "lastWriteMonotonic": time.monotonic() - 60,
            "node": SimpleNamespace(config=SimpleNamespace(id="edge")),
        }
    ]
    sampler.require_healthy()


def test_background_thread_exit_without_reported_error_is_fault():
    from types import SimpleNamespace

    from nanolab.one_shot.supervision import check_background_worker

    worker = SimpleNamespace(
        require_healthy=lambda: None,
        thread=SimpleNamespace(is_alive=lambda: False),
        stopping=False,
    )
    with pytest.raises(RuntimeError, match="thread stopped"):
        check_background_worker(worker)


def test_all_campaign_phases_and_nine_intervals_use_sonata_events(tmp_path):
    supervisor = json_supervision(tmp_path, cadence=0.01)
    releases = []
    resource = Resource(
        title="Owned VM",
        acquire=lambda _: True,
        release=lambda *_: releases.append(True),
    )
    workflow = Workflow("all-phases")
    phases = [
        "Provision",
        "Cloud-init",
        "Services",
        "Preflight",
        "Replicas",
        "Precheck",
        "Warmup",
        *[f"Interval {i}" for i in range(9)],
        "Report",
    ]
    for title in phases:
        task = Action(lambda inputs: inputs.resource(resource))
        task.title = title
        workflow.add(task, requires=(resource,))
    supervisor.run(workflow.run)
    events = [
        json.loads(line)
        for line in (tmp_path / "supervision.jsonl").read_text().splitlines()
    ]
    passed = [row["title"] for row in events if row["kind"] == "task.passed"]
    assert all(title in passed for title in phases)
    assert releases == [True]


def test_signal_handlers_remain_installed_during_archive(tmp_path, monkeypatch):
    from nanolab.one_shot import supervision

    output = tmp_path / "raw"
    output.mkdir()
    archive = tmp_path / "archive"
    previous = signal.getsignal(signal.SIGTERM)
    copy = supervision.shutil.copytree

    def inspect_copy(source, target, **kwargs):
        assert signal.getsignal(signal.SIGTERM) != previous
        return copy(source, target, **kwargs)

    monkeypatch.setattr(supervision.shutil, "copytree", inspect_copy)
    json_supervision(output, archive=archive, cadence=0.01).run(lambda: None)
    assert signal.getsignal(signal.SIGTERM) == previous


def test_archive_waits_for_owned_writer_cleanup(tmp_path):
    from threading import Event, Thread
    from types import SimpleNamespace

    stop = Event()
    thread = Thread(target=stop.wait)
    thread.start()
    archive = tmp_path.parent / "unsafe-archive"
    supervisor = json_supervision(tmp_path, archive=archive, cadence=0.01)
    resource = supervisor.resource(
        Resource(
            title="Broken collector cleanup",
            acquire=lambda _: SimpleNamespace(thread=thread),
            release=lambda *_: None,
        ),
        "collector",
        lambda _: None,
    )
    workflow = Workflow("unjoined-writer")
    workflow.add(Action(lambda _: None), requires=(resource,))
    try:
        with pytest.raises(RuntimeError, match="writer still running"):
            supervisor.run(workflow.run)
        assert not archive.exists()
        assert json.loads((tmp_path / "result.json").read_text())["exitCode"] == 1
    finally:
        stop.set()
        thread.join()


def test_late_health_signal_still_archives_and_retains_first_fault(
    tmp_path, monkeypatch
):
    from threading import Event

    from nanolab.one_shot import supervision

    detected = Event()
    killed = supervision.os.kill
    supervisor = json_supervision(
        tmp_path, archive=tmp_path.parent / "late-archive", cadence=0.01
    )

    def delayed_signal(pid, signum):
        detected.set()
        time.sleep(0.05)
        killed(pid, signum)

    monkeypatch.setattr(supervision.os, "kill", delayed_signal)
    releases = []

    def unhealthy(_value):
        raise RuntimeError("late health fault")

    resource = supervisor.resource(
        Resource(
            title="Worker",
            acquire=lambda _: True,
            release=lambda *_: releases.append(True),
        ),
        "worker",
        unhealthy,
    )
    workflow = Workflow("late-health")
    workflow.add(Action(lambda _: detected.wait(timeout=1)), requires=(resource,))
    with pytest.raises(RuntimeError, match="late health fault"):
        supervisor.run(workflow.run)
    assert releases == [True]
    assert json.loads((tmp_path / "result.json").read_text())["exitCode"] == 1
    assert supervisor.archive is not None
    assert (supervisor.archive / "result.json").exists()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_first_archive_signal_defers_cleanup_and_returns_interruption(
    tmp_path, monkeypatch, signum
):
    from nanolab.one_shot import supervision

    output = tmp_path / "raw"
    output.mkdir()
    archive = tmp_path / "archive"
    copy = supervision.shutil.copytree

    def interrupted_copy(source, target, **kwargs):
        os.kill(os.getpid(), signum)
        return copy(source, target, **kwargs)

    monkeypatch.setattr(supervision.shutil, "copytree", interrupted_copy)
    with pytest.raises(RuntimeError, match="interrupted"):
        json_supervision(output, archive=archive, cadence=0.01).run(lambda: None)
    result = json.loads((archive / "result.json").read_text())
    assert result["exitCode"] == 128 + signum
    assert result["archiveVerified"] is True


def test_successful_generator_completion_with_rejects_is_not_fault(tmp_path):
    from types import SimpleNamespace

    supervisor = json_supervision(tmp_path, cadence=0.01)
    child = cast(Any, SimpleNamespace(poll=lambda: 0))
    # All originals returned; rejected requests are successful generator work.
    CampaignSupervision.check_process(
        child, expected_end=time.monotonic() + 60, completed=lambda: True
    )
    with pytest.raises(RuntimeError, match="exit 0"):
        CampaignSupervision.check_process(
            child, expected_end=time.monotonic() + 60, completed=lambda: False
        )


@pytest.mark.parametrize(
    ("elapsed", "phase", "epoch"),
    [
        (-800, "setup", None),
        (-100, "warmup", None),
        (100, "campaign", 0),
        (3400, "drain", None),
    ],
)
def test_heartbeat_uses_exact_frozen_phase_and_includes_scope(
    tmp_path, elapsed, phase, epoch
):
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "mode": "oracle",
                "period_seconds": 360,
                "parameters": {
                    "generator": {"warmupSeconds": 720},
                    "functions": {"f": {}},
                    "logicalSchedule": {
                        "anchorMonotonicSeconds": time.monotonic() - elapsed,
                        "epochCount": 9,
                    },
                },
            }
        )
    )
    supervisor = json_supervision(tmp_path, cadence=0.01)
    supervisor.run(lambda: time.sleep(0.03))
    rows = [
        json.loads(line)
        for line in (tmp_path / "supervision.jsonl").read_text().splitlines()
    ]
    heartbeat = next(row for row in rows if row["kind"] == "heartbeat")
    assert heartbeat["observation"] == {
        "mode": "oracle",
        "phase": phase,
        "epoch": epoch,
        "functions": ["f"],
    }


def test_status_io_failure_interrupts_real_workflow_and_preserves_cleanup(
    tmp_path, monkeypatch
):
    supervisor = json_supervision(tmp_path, cadence=0.01)
    released = []
    completed = []
    resource = supervisor.resource(
        Resource(
            title="Owned resource during status failure",
            acquire=lambda _: "owned",
            release=lambda _, value: released.append(value),
        ),
        "owned",
        lambda _: None,
    )
    original = supervisor.record

    def fail_heartbeat(kind, **values):
        if kind in {"heartbeat", "operational.failure"}:
            raise OSError("status disk unavailable")
        return original(kind, **values)

    monkeypatch.setattr(supervisor, "record", fail_heartbeat)
    workflow = Workflow("status-write-fault")

    def action(_):
        time.sleep(0.2)
        completed.append(True)

    workflow.add(Action(action), requires=(resource,))
    with pytest.raises(OSError, match="status disk unavailable"):
        supervisor.run(workflow.run)
    assert not completed
    assert released == ["owned"]
    assert not supervisor.thread.is_alive()


def test_service_watch_covers_resource_lifetime_before_sampler(tmp_path):
    released = []
    supervisor = json_supervision(tmp_path, cadence=0.005)
    calls = []

    def probe(_):
        calls.append(True)
        if len(calls) > 1:
            raise RuntimeError("owned service exited")

    base = Resource(
        title="Owned service",
        acquire=lambda _: "service",
        release=lambda _, value: released.append(value),
    )
    watched = supervisor.service_resource(
        base, "control-plane", probe, interval_seconds=0.01
    )
    workflow = Workflow("service-lifetime")
    workflow.add(Action(lambda _: time.sleep(0.2)), requires=(watched,))
    with pytest.raises(RuntimeError, match="control-plane"):
        supervisor.run(workflow.run)
    assert released == ["service"]
    assert not supervisor.checks


@pytest.mark.parametrize("failure_kind", ["stdout", "status", "initial"])
def test_reporting_io_failure_never_prevents_sonata_release(
    tmp_path, monkeypatch, failure_kind
):
    supervisor = json_supervision(tmp_path, cadence=0.005)
    released = []
    resource = supervisor.resource(
        Resource(
            title="Owned VM",
            acquire=lambda _: "vm",
            release=lambda _, value: released.append(value),
        ),
        "vm",
        lambda _: None,
    )
    original = supervisor._write_status
    failed = []

    def should_fail():
        if "Simulated campaign phase" in supervisor.active.values():
            failed.append(True)
        return bool(failed)

    def failing_status():
        if failure_kind == "initial" or should_fail():
            raise OSError("status unavailable")
        original()

    def failing_stdout(*args, **kwargs):
        if should_fail():
            raise BrokenPipeError("stdout unavailable")

    if failure_kind == "stdout":
        monkeypatch.setattr("builtins.print", failing_stdout)
    else:
        monkeypatch.setattr(supervisor, "_write_status", failing_status)
    workflow = Workflow("reporting-failure")
    workflow.add(Action(lambda _: time.sleep(0.1)), requires=(resource,))
    with pytest.raises((OSError, BrokenPipeError)):
        supervisor.run(workflow.run)
    assert released == ([] if failure_kind == "initial" else ["vm"])
    assert not supervisor.thread.is_alive()


@pytest.mark.parametrize("cp_present", [True, False])
def test_inventory_liveness_is_independent_of_cgroup_measurements(tmp_path, cp_present):
    from types import SimpleNamespace

    from nanolab.one_shot.supervision import check_sampler

    path = tmp_path / "node.jsonl"
    inventory = [{"role": "collector"}, {"role": "function"}]
    if cp_present:
        inventory.append({"role": "control-plane"})
    path.write_text(
        json.dumps(
            {
                "expectedContainerIds": ["collector", "function"]
                + (["cp"] if cp_present else []),
                "containerInventory": inventory,
                "containers": [{"role": "collector"}],
                "errors": [
                    {
                        "containerId": "cp" if cp_present else "function",
                        "error": "cgroup unreadable",
                    }
                ],
            }
        )
        + "\n"
    )
    session = SimpleNamespace(
        require_healthy=lambda: None,
        sessions=[
            {
                "output": SimpleNamespace(name=str(path)),
                "node": SimpleNamespace(config=SimpleNamespace(id="edge")),
            }
        ],
    )
    if cp_present:
        check_sampler(session)
    else:
        with pytest.raises(RuntimeError, match="control-plane"):
            check_sampler(session)


@pytest.mark.parametrize("fails", [False, True])
def test_terminal_outcome_updates_report_before_verified_archive(tmp_path, fails):
    from nanolab.one_shot.report import render_report
    from tests.one_shot.test_report import manifest

    raw, archive = tmp_path / "raw", tmp_path / "archive"
    raw.mkdir()
    supervisor = json_supervision(raw, archive=archive)

    def action():
        render_report(manifest(), None, raw / "report.html")
        if fails:
            raise RuntimeError("experiment process stopped")

    if fails:
        with pytest.raises(RuntimeError, match="experiment process stopped"):
            supervisor.run(action)
    else:
        supervisor.run(action)
    for directory in (raw, archive):
        outcome = json.loads((directory / "report.json").read_text())[
            "operationalOutcome"
        ]
        assert outcome["state"] == ("failed" if fails else "completed")
        assert outcome["exitCode"] == int(fails)
        assert outcome["archiveVerified"] is True
        assert "not-finalized" not in (directory / "report.html").read_text()


@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize("release_fails", [False, True])
def test_service_initial_probe_failure_survives_cleanup_errors(
    tmp_path, monkeypatch, close_fails, release_fails
):
    """Service initial probe failure survives cleanup errors."""
    from nanolab.tasks.one_shot.clock import ClockMonitor

    supervisor = json_supervision(tmp_path, cadence=100)
    released = []
    close = ClockMonitor.close

    def failing_close(worker):
        close(worker)
        if close_fails:
            raise RuntimeError("secondary close failed")

    def release(_, value):
        released.append(value)
        if release_fails:
            raise RuntimeError("secondary delete failed")

    def probe(value):
        assert value == "service"
        (tmp_path / "raw.txt").write_text("retained")
        raise RuntimeError("primary readiness failed")

    monkeypatch.setattr(ClockMonitor, "close", failing_close)
    watched = supervisor.service_resource(
        Resource(title="Service", acquire=lambda _: "service", release=release),
        "service",
        probe,
    )
    workflow = Workflow("initial-probe")
    workflow.add(Action(lambda _: pytest.fail("must not run")), requires=(watched,))
    with pytest.raises(RuntimeError, match="primary readiness failed") as caught:
        supervisor.run(workflow.run)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["cause"] == "primary readiness failed"
    assert result["notes"] == getattr(caught.value, "__notes__", [])
    for fails, message in (
        (close_fails, "secondary close failed"),
        (release_fails, "secondary delete failed"),
    ):
        assert (message in str(result["notes"])) is fails
    assert released == ["service"]
    assert (tmp_path / "raw.txt").read_text() == "retained"


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_service_cleanup_preserves_interruption_and_dependencies(
    tmp_path, monkeypatch, signum
):
    """Service cleanup preserves interruption and dependencies."""
    from nanolab.tasks.one_shot.clock import ClockMonitor

    supervisor = json_supervision(tmp_path, cadence=100)
    calls = []
    base = Resource(
        title="VM",
        acquire=lambda _: calls.append("acquire vm") or "vm",
        release=lambda _, value: calls.append("release " + value),
    )
    unused = Resource(
        title="Unacquired",
        acquire=lambda _: pytest.fail("unused"),
        release=lambda *_: pytest.fail("unowned release"),
    )
    close = ClockMonitor.close

    def failing_close(worker):
        close(worker)
        raise RuntimeError("secondary close failed")

    def acquire(inputs):
        assert inputs.resource(base) == "vm"
        calls.append("acquire service")
        return "service"

    def release(_, value):
        calls.append("release " + value)
        raise RuntimeError("secondary delete failed")

    def interrupt(_):
        os.kill(os.getpid(), signum)

    monkeypatch.setattr(ClockMonitor, "close", failing_close)
    service = supervisor.service_resource(
        Resource(title="Service", acquire=acquire, release=release, requires=(base,)),
        "service",
        interrupt,
    )
    assert service.requires == (base,)
    workflow = Workflow("probe-interruption")
    workflow.add(Action(lambda _: None), requires=(service,))
    workflow.add(Action(lambda _: None), requires=(unused,))
    with pytest.raises(RuntimeError, match="interrupted"):
        supervisor.run(workflow.run)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["exitCode"] == 128 + signum
    assert "secondary close failed" in str(result["notes"])
    assert "secondary delete failed" in str(result["notes"])
    assert calls == ["acquire vm", "acquire service", "release service", "release vm"]


def test_service_normal_release_retains_first_cleanup_error(tmp_path, monkeypatch):
    """Service normal release retains first cleanup error."""
    from nanolab.tasks.one_shot.clock import ClockMonitor

    supervisor = json_supervision(tmp_path, cadence=100)
    released = []
    close = ClockMonitor.close

    first = RuntimeError("first close failed")

    def failing_close(worker):
        close(worker)
        raise first

    def release(_, value):
        released.append(value)
        raise RuntimeError("secondary delete failed")

    monkeypatch.setattr(ClockMonitor, "close", failing_close)
    service = supervisor.service_resource(
        Resource(title="Service", acquire=lambda _: "service", release=release),
        "service",
        lambda _: None,
    )
    workflow = Workflow("normal-release")
    workflow.add(Action(lambda inputs: inputs.resource(service)), requires=(service,))
    with pytest.raises(RuntimeError, match="first close failed") as caught:
        supervisor.run(workflow.run)
    assert "first close failed" in str(caught.value)
    assert "secondary delete failed" in str(first.__notes__)
    assert released == ["service"]
    assert not supervisor.checks


@pytest.mark.parametrize("corrupt", [False, True])
def test_final_archive_hash_failure_invalidates_both_receipts(
    tmp_path, monkeypatch, capsys, corrupt
):
    """Final archive hash failure invalidates both receipts."""
    from nanolab.one_shot import supervision
    from nanolab.one_shot.report import render_report
    from tests.one_shot.test_report import manifest

    raw, archive = tmp_path / "raw", tmp_path / "archive"
    raw.mkdir()
    (raw / "measurement.json").write_text('{"value":1}')
    supervisor = json_supervision(raw, archive=archive, cadence=100)
    copy = supervision.shutil.copy2

    def corrupt_measurement(source, target, **kwargs):
        value = copy(source, target, **kwargs)
        if corrupt and Path(source).name == "supervision.jsonl":
            (archive / "measurement.json").write_text('{"value":2}')
        return value

    from pathlib import Path

    monkeypatch.setattr(supervision.shutil, "copy2", corrupt_measurement)

    def action():
        render_report(manifest(), None, raw / "report.html")

    if corrupt:
        with pytest.raises(
            ValueError, match=r"archive hash mismatch: measurement\.json"
        ):
            supervisor.run(action)
    else:
        supervisor.run(action)
    for directory in (raw, archive):
        for name in ("status.json", "result.json"):
            receipt = json.loads((directory / name).read_text())
            assert receipt["state"] == ("failed" if corrupt else "completed")
            assert receipt["exitCode"] == int(corrupt)
            assert receipt["archiveVerified"] is (not corrupt)
        report = json.loads((directory / "report.json").read_text())
        assert report["operationalOutcome"]["state"] == receipt["state"]
        if corrupt:
            assert receipt["cause"] in (directory / "report.html").read_text()
    assert (raw / "measurement.json").read_text() == '{"value":1}'
    assert (archive / "measurement.json").read_text() == (
        '{"value":2}' if corrupt else '{"value":1}'
    )
    if not corrupt:
        supervisor._verify_archive()
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["state"] == (
        "failed" if corrupt else "completed"
    )


@pytest.mark.parametrize(
    "primary", [None, "workflow failed", signal.SIGINT, signal.SIGTERM]
)
@pytest.mark.parametrize("boundary", ["preexisting", "partial", "invalidation"])
def test_archive_failure_preserves_primary_and_owned_destination(
    tmp_path, monkeypatch, capsys, primary, boundary
):
    """Archive failure preserves primary and owned destination."""
    from pathlib import Path

    from nanolab.one_shot import supervision

    raw, archive = tmp_path / "raw", tmp_path / "archive"
    raw.mkdir()
    (raw / "measurement.txt").write_text("retained")
    supervisor = json_supervision(raw, archive=archive, cadence=100)
    copytree, copy2 = supervision.shutil.copytree, supervision.shutil.copy2
    if boundary == "preexisting":
        archive.mkdir()
        (archive / "result.json").write_text("unowned bytes")
        before = {p.name: p.read_bytes() for p in archive.iterdir()}

    def copy_tree(source, target, **kwargs):
        value = copytree(source, target, **kwargs)
        if boundary == "partial":
            raise OSError("partial archive failed")
        return value

    def copy_control(source, target, **kwargs):
        if boundary == "invalidation" and supervisor.state["archiveVerified"] is False:
            raise OSError("invalidation storage unavailable")
        value = copy2(source, target, **kwargs)
        if boundary == "invalidation" and Path(source).name == "supervision.jsonl":
            (archive / "measurement.txt").write_text("corrupted")
        return value

    def fail(_):
        if isinstance(primary, int):
            os.kill(os.getpid(), primary)
        elif primary is not None:
            raise RuntimeError(primary)

    monkeypatch.setattr(supervision.shutil, "copytree", copy_tree)
    monkeypatch.setattr(supervision.shutil, "copy2", copy_control)
    workflow = Workflow("archive-failure")
    workflow.add(Action(fail))
    with pytest.raises((RuntimeError, OSError, ValueError)):
        supervisor.run(workflow.run)
    result = json.loads((raw / "result.json").read_text())
    assert result["state"] == "failed" and result["archiveVerified"] is False
    assert supervisor.primary is not None
    assert result["cause"] == str(supervisor.primary)
    if primary is not None:
        assert ("interrupted" if isinstance(primary, int) else primary) in result[
            "cause"
        ]
        assert "archive:" in str(result["notes"])
    assert result["exitCode"] == (128 + primary if isinstance(primary, int) else 1)
    assert (raw / "measurement.txt").read_text() == "retained"
    if boundary == "preexisting":
        assert {p.name: p.read_bytes() for p in archive.iterdir()} == before
    elif boundary == "partial":
        copied = json.loads((archive / "result.json").read_text())
        assert copied["state"] == "failed" and copied["archiveVerified"] is False
    else:
        assert "archive invalidation" in str(result["notes"])
        assert "invalidation storage unavailable" in str(result["notes"])
    stdout = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert stdout["state"] == "failed"
    if boundary == "invalidation":
        assert "archive invalidation" in str(stdout["notes"])


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("late", [False, True])
def test_deferred_signal_final_verification_invalidates_archive(
    tmp_path, monkeypatch, signum, late
):
    """Deferred signal final verification invalidates archive."""
    from pathlib import Path

    from nanolab.one_shot import supervision

    raw, archive = tmp_path / "raw", tmp_path / "archive"
    raw.mkdir()
    (raw / "measurement.txt").write_text("retained")
    supervisor = json_supervision(raw, archive=archive, cadence=100)
    copy = supervision.shutil.copy2
    verify = supervisor._verify_archive
    checks = []

    def control_copy(source, target, **kwargs):
        value = copy(source, target, **kwargs)
        if Path(source).name == "supervision.jsonl" and not checks:
            os.kill(os.getpid(), signum)
            if not late:
                (archive / "measurement.txt").write_text("corrupted")
            checks.append(True)
        return value

    verifications = []

    def verify_then_interrupt():
        verify()
        verifications.append(True)
        if late and len(verifications) == 2:
            (archive / "measurement.txt").write_text("corrupted")

    monkeypatch.setattr(supervision.shutil, "copy2", control_copy)
    monkeypatch.setattr(supervisor, "_verify_archive", verify_then_interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        supervisor.run(lambda: None)
    for directory in (raw, archive):
        result = json.loads((directory / "result.json").read_text())
        assert result["exitCode"] == 128 + signum
        assert result["archiveVerified"] is False
        assert "archive hash mismatch" in str(result["notes"])


def test_archive_failure_survives_unwritable_failure_receipts(
    tmp_path, monkeypatch, capsys
):
    """Archive failure survives unwritable failure receipts."""
    from pathlib import Path

    from nanolab.one_shot import supervision

    raw, archive = tmp_path / "raw", tmp_path / "archive"
    raw.mkdir()
    (raw / "measurement.txt").write_text("retained")
    supervisor = json_supervision(raw, archive=archive, cadence=100)
    copy = supervision.shutil.copy2
    write = Path.write_text
    failed = []

    def corrupt(source, target, **kwargs):
        result = copy(source, target, **kwargs)
        if Path(source).name == "supervision.jsonl":
            (archive / "measurement.txt").write_text("corrupted")
            failed.append(True)
        return result

    def reject_receipt(path, text, *args, **kwargs):
        if (
            failed
            and path.parent == raw
            and path.name in {"status.json.tmp", "status.json", "result.json"}
        ):
            raise OSError("raw failure receipt unavailable")
        return write(path, text, *args, **kwargs)

    monkeypatch.setattr(supervision.shutil, "copy2", corrupt)
    monkeypatch.setattr(Path, "write_text", reject_receipt)
    with pytest.raises(ValueError, match="archive hash mismatch"):
        supervisor.run(lambda: None)
    final = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert final["state"] == "failed" and final["archiveVerified"] is False
    assert "raw failure receipt unavailable" in str(final["notes"])
    assert (raw / "measurement.txt").read_text() == "retained"
    assert (archive / "measurement.txt").read_text() == "corrupted"

    for name in ("status.json", "result.json"):
        receipt = json.loads((archive / name).read_text())
        assert receipt["state"] == "failed" and receipt["archiveVerified"] is False
        assert "raw failure receipt unavailable" in str(receipt["notes"])


@pytest.mark.parametrize("blocked", ["result.json", "status.json"])
def test_terminal_persistence_failure_updates_writable_reports(tmp_path, blocked):
    """A real failed terminal write must invalidate every writable report."""
    from nanolab.one_shot.report import render_report
    from tests.one_shot.test_report import manifest

    supervisor = json_supervision(tmp_path, cadence=100)

    def action():
        render_report(manifest(), None, tmp_path / "report.html")
        if blocked == "status.json":
            (tmp_path / blocked).unlink()
        (tmp_path / blocked).mkdir()

    with pytest.raises(OSError, match="Is a directory"):
        supervisor.run(action)
    report = json.loads((tmp_path / "report.json").read_text())
    outcome = report["operationalOutcome"]
    assert outcome["state"] == "failed" and outcome["exitCode"] == 1
    assert outcome["archiveVerified"] is False
    assert "result persistence" in str(outcome["notes"])
    assert (
        "&quot;state&quot;: &quot;failed&quot;"
        in (tmp_path / "report.html").read_text()
    )
    assert supervisor.primary is not None and outcome["cause"] == str(
        supervisor.primary
    )


@pytest.mark.parametrize("signum", [None, signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("workflow_fails", [False, True])
def test_signal_during_report_finalization_updates_saved_outcomes(
    tmp_path, monkeypatch, capsys, signum, archived, workflow_fails
):
    """Persist late real signals without changing primary failure or cleanup."""
    import html
    import re
    from pathlib import Path

    from nanolab.one_shot.report import render_report
    from tests.one_shot.test_report import manifest

    raw, archive = tmp_path / "raw", tmp_path / "archive" if archived else None
    raw.mkdir()
    supervisor = json_supervision(raw, archive=archive, cadence=100)
    releases, delivered = [], []
    resource = Resource(
        title="Simulated owned VM",
        acquire=lambda _: "vm",
        release=lambda _, value: releases.append(value),
    )

    def action(inputs):
        assert inputs.resource(resource) == "vm"
        (raw / "measurement.txt").write_text("retained measurement")
        render_report(manifest(), None, raw / "report.html")
        if workflow_fails:
            raise RuntimeError("primary workflow failed")

    workflow = Workflow("signal-during-finalization")
    workflow.add(Action(action), requires=(resource,))
    write = Path.write_text

    def write_and_signal(path, data, *args, **kwargs):
        if (
            signum is not None
            and path == raw / "report.html"
            and supervisor.finishing
            and not delivered
        ):
            delivered.append(signum)
            os.kill(os.getpid(), signum)
        return write(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_and_signal)
    if signum is not None or workflow_fails:
        with pytest.raises(
            RuntimeError,
            match="primary workflow failed" if workflow_fails else "interrupted",
        ):
            supervisor.run(workflow.run)
    else:
        supervisor.run(workflow.run)
    code = 1 if workflow_fails else 128 + signum if signum is not None else 0
    final = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert final["exitCode"] == code
    assert releases == ["vm"]
    assert delivered == ([] if signum is None else [signum])
    for directory in (raw, archive):
        if directory is None:
            continue
        outcomes = [
            json.loads((directory / name).read_text())
            for name in ("result.json", "status.json")
        ]
        outcomes.append(
            json.loads((directory / "report.json").read_text())["operationalOutcome"]
        )
        document = (directory / "report.html").read_text()
        outcomes.append(
            json.loads(
                html.unescape(re.findall(r"<pre>(.*?)</pre>", document, re.S)[0])
            )["operationalOutcome"]
        )
        for outcome in outcomes:
            assert outcome["exitCode"] == code
            assert outcome["cause"] == final["cause"]
            assert outcome["state"] == ("failed" if code else "completed")
        assert (directory / "measurement.txt").read_text() == "retained measurement"
    if archive is not None:
        assert final["archiveVerified"] is True
        supervisor._verify_archive()


@pytest.mark.parametrize("blocked", [None, "report.html", "report.json"])
@pytest.mark.parametrize(
    "primary", [None, "workflow failed", signal.SIGINT, signal.SIGTERM]
)
def test_archive_report_failure_updates_writable_outcomes(
    tmp_path, monkeypatch, capsys, blocked, primary
):
    from pathlib import Path

    from nanolab.one_shot.report import render_report
    from tests.one_shot.test_report import embedded_summary, manifest

    raw, archive = tmp_path / "raw", tmp_path / "archive"
    raw.mkdir()
    supervisor = json_supervision(raw, archive=archive, cadence=100)
    write = Path.write_text
    armed, releases = [], []
    resource = Resource(
        title="Owned fake resource",
        acquire=lambda _: "vm",
        release=lambda _, value: releases.append(value),
    )

    def reject_late_write(path, data, *args, **kwargs):
        if blocked is not None and path == raw / blocked and armed:
            raise OSError("raw " + blocked + " write unavailable")
        return write(path, data, *args, **kwargs)

    verify = supervisor._verify_archive
    verifications = []

    def verify_and_arm():
        verify()
        verifications.append(True)
        if len(verifications) == 1:
            armed.append(True)

    monkeypatch.setattr(Path, "write_text", reject_late_write)
    monkeypatch.setattr(supervisor, "_verify_archive", verify_and_arm)

    def action(inputs):
        assert inputs.resource(resource) == "vm"
        (raw / "measurement.txt").write_text("retained")
        render_report(manifest(), None, raw / "report.html")
        if isinstance(primary, int):
            os.kill(os.getpid(), primary)
        elif primary is not None:
            raise RuntimeError(primary)

    workflow = Workflow("report-write-during-archive-finalization")
    workflow.add(Action(action), requires=(resource,))
    if blocked is not None or primary is not None:
        with pytest.raises((RuntimeError, OSError)):
            supervisor.run(workflow.run)
    else:
        supervisor.run(workflow.run)
    final = json.loads(capsys.readouterr().out.splitlines()[-1])
    expected_code = (
        128 + primary
        if isinstance(primary, int)
        else int(blocked is not None or primary is not None)
    )
    assert final["exitCode"] == expected_code
    if primary is not None:
        assert ("interrupted" if isinstance(primary, int) else primary) in final[
            "cause"
        ]
    assert final["archiveVerified"] is (blocked is None)
    assert releases == ["vm"]
    for directory in (raw, archive):
        for name in ("result.json", "status.json", "report.json", "report.html"):
            if directory == raw and name == blocked:
                continue
            document = (directory / name).read_text()
            outcome = (
                embedded_summary(document)["operationalOutcome"]
                if name.endswith("html")
                else json.loads(document)
            )
            if name == "report.json":
                outcome = outcome["operationalOutcome"]
            assert outcome["exitCode"] == expected_code
            assert outcome["state"] == ("failed" if expected_code else "completed")
            assert outcome["cause"] == final["cause"]
            assert outcome["archiveVerified"] is (blocked is None)
        assert (directory / "measurement.txt").read_text() == "retained"
    if blocked is not None:
        assert blocked in str(final["notes"])
        assert len(verifications) == 1
    else:
        verify()


@pytest.mark.parametrize("archive_blocked", ["report.json", "report.html"])
def test_archive_report_invalidation_attempts_other_writable_outputs(
    tmp_path, monkeypatch, capsys, archive_blocked
):
    from pathlib import Path

    from nanolab.one_shot.report import render_report
    from tests.one_shot.test_report import embedded_summary, manifest

    raw, archive = tmp_path / "raw", tmp_path / "archive"
    raw.mkdir()
    supervisor = json_supervision(raw, archive=archive, cadence=100)
    write = Path.write_text
    copy = __import__("shutil").copy2
    armed = []

    def reject_write(path, data, *a, **kw):
        if armed and (path == raw / "report.html" or path == archive / archive_blocked):
            raise OSError(str(path) + " unavailable")
        return write(path, data, *a, **kw)

    def reject_copy(source, target, **kw):
        if armed and Path(target) == archive / archive_blocked:
            raise OSError(str(target) + " unavailable")
        return copy(source, target, **kw)

    verify = supervisor._verify_archive

    def verify_and_arm():
        verify()
        armed.append(True)

    monkeypatch.setattr(Path, "write_text", reject_write)
    monkeypatch.setattr("shutil.copy2", reject_copy)
    monkeypatch.setattr(supervisor, "_verify_archive", verify_and_arm)
    workflow = Workflow("archive-output-unavailable")
    workflow.add(Action(lambda _: render_report(manifest(), None, raw / "report.html")))
    with pytest.raises(OSError, match="unavailable"):
        supervisor.run(workflow.run)
    final = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert final["archiveVerified"] is False
    assert archive_blocked in str(final["notes"])
    other = "report.json" if archive_blocked == "report.html" else "report.html"
    summary = (
        json.loads((archive / other).read_text())
        if other.endswith("json")
        else embedded_summary((archive / other).read_text())
    )
    assert summary["operationalOutcome"]["state"] == "failed"
    for directory in (raw, archive):
        assert json.loads((directory / "result.json").read_text())["state"] == "failed"


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("blocked_at", ["result.json", "status.json.tmp"])
@pytest.mark.parametrize("stage", ["raw", "archive"])
def test_signal_during_receipt_write_persists_changed_snapshot(
    tmp_path, monkeypatch, capsys, signum, blocked_at, stage
):
    from pathlib import Path

    from nanolab.one_shot.report import render_report
    from tests.one_shot.test_report import embedded_summary, manifest

    raw, archive = (
        tmp_path / "raw",
        tmp_path / "archive" if stage == "archive" else None,
    )
    raw.mkdir()
    supervisor = json_supervision(raw, archive=archive, cadence=100)
    write = Path.write_text
    delivered, releases, writing = [], [], []
    persist = supervisor._write_result

    def persist_receipts():
        writing.append(True)
        try:
            persist()
        finally:
            writing.pop()

    monkeypatch.setattr(supervisor, "_write_result", persist_receipts)
    resource = Resource(
        title="Owned fake VM",
        acquire=lambda _: "vm",
        release=lambda _, value: releases.append(value),
    )

    def interrupt_write(path, data, *a, **kw):
        if (
            writing
            and supervisor.finishing
            and not delivered
            and path == raw / blocked_at
            and (supervisor.state["archiveVerified"] if stage == "archive" else True)
        ):
            delivered.append(signum)
            os.kill(os.getpid(), signum)
        return write(path, data, *a, **kw)

    monkeypatch.setattr(Path, "write_text", interrupt_write)
    workflow = Workflow("signal-during-receipt-persistence")
    workflow.add(
        Action(lambda _: render_report(manifest(), None, raw / "report.html")),
        requires=(resource,),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        supervisor.run(workflow.run)
    final = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert final["exitCode"] == 128 + signum
    assert releases == ["vm"] and delivered == [signum]
    for directory in (raw, archive):
        if directory is None:
            continue
        for name in ("result.json", "status.json", "report.json", "report.html"):
            text = (directory / name).read_text()
            outcome = (
                embedded_summary(text)["operationalOutcome"]
                if name.endswith("html")
                else json.loads(text)
            )
            if name == "report.json":
                outcome = outcome["operationalOutcome"]
            assert outcome["exitCode"] == 128 + signum
            assert outcome["state"] == "failed" and outcome["cause"] == final["cause"]
    if archive is not None:
        supervisor._verify_archive()


@pytest.mark.parametrize("archived", [False, True])
def test_primary_failure_retains_both_report_channel_limitations(
    tmp_path, monkeypatch, capsys, archived
):
    from pathlib import Path

    from nanolab.one_shot.report import render_report
    from tests.one_shot.test_report import manifest

    raw, archive = tmp_path / "raw", tmp_path / "archive" if archived else None
    raw.mkdir()
    supervisor = json_supervision(raw, archive=archive, cadence=100)
    write = Path.write_text
    attempts = []

    def reject_reports(path, data, *a, **kw):
        if (
            supervisor.finishing
            and path.parent == raw
            and path.name in {"report.html", "report.json"}
        ):
            attempts.append(path.name)
            raise OSError(path.name + " unavailable")
        return write(path, data, *a, **kw)

    def fail(_):
        render_report(manifest(), None, raw / "report.html")
        raise RuntimeError("primary workflow failed")

    monkeypatch.setattr(Path, "write_text", reject_reports)
    workflow = Workflow("primary-with-two-report-failures")
    workflow.add(Action(fail))
    with pytest.raises(RuntimeError, match="primary workflow failed"):
        supervisor.run(workflow.run)
    final = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert final["cause"] == "primary workflow failed" and final["exitCode"] == 1
    assert {"report.html", "report.json"} <= set(attempts)
    for outcome in (
        final,
        json.loads((raw / "result.json").read_text()),
        json.loads((raw / "status.json").read_text()),
    ):
        assert "report.html unavailable" in str(outcome["notes"])
        assert "report.json unavailable" in str(outcome["notes"])
