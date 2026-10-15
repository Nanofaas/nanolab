"""Real Sonata delivery, view isolation and campaign fault boundaries."""

import json
import os
import signal
import time
from contextlib import contextmanager
from threading import Thread

import pytest
from sonata_engine import Resource, Workflow, bind_workflow_sink
from sonata_engine.workflow.context import active_sink
from sonata_engine.workflow.reporting import status, subtask, workflow_log

from nanolab.one_shot.supervision import CampaignSupervision
from tests.one_shot.test_supervision import Action


class View:
    def __init__(self):
        self.events = []
        self.rows = []
        self.statuses = []

    def emit(self, event):
        self.events.append(event)

    def campaign_event(self, row):
        self.rows.append(row)

    @contextmanager
    def status(self, label):
        self.statuses.append((label, "start"))
        try:
            yield
        finally:
            self.statuses.append((label, "end"))


@pytest.mark.parametrize("ending", [None, "failure", signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("explicit", [False, True])
def test_outer_view_receives_real_events_once_and_binding_is_restored(
    tmp_path, ending, explicit
):
    view = View()
    supervisor = CampaignSupervision(
        tmp_path, **({"presentation": view} if explicit else {})
    )
    releases = []
    resource = Resource(
        title="Owned VM",
        acquire=lambda _: "vm",
        release=lambda _, v: releases.append(v),
    )
    workflow = Workflow("composed")

    def action(_):
        with (
            status("outer"),
            status("inner"),
            subtask(task_id="nested", title="Nested"),
        ):
            worker = Thread(
                target=lambda: workflow_log("worker output", stream="stderr")
            )
            worker.start()
            worker.join()
        if ending == "failure":
            raise ValueError("task failed")
        if ending is not None:
            os.kill(os.getpid(), ending)

    workflow.add(Action(action), requires=(resource,))
    with bind_workflow_sink(view):
        if ending is None:
            supervisor.run(workflow.run)
        else:
            with pytest.raises((ValueError, RuntimeError)):
                supervisor.run(workflow.run)
        assert active_sink() is view
    assert active_sink() is None
    assert releases == ["vm"]
    rows = [
        json.loads(s) for s in (tmp_path / "supervision.jsonl").read_text().splitlines()
    ]
    events = [
        r for r in rows if r["kind"].startswith("task.") or r["kind"] == "log.line"
    ]
    assert len(events) == len(view.events) > 0
    assert [
        (r["kind"], r["task_id"], r["parent_task_id"], r["at"]) for r in events
    ] == [(e.kind, e.task_id, e.parent_task_id, e.at.isoformat()) for e in view.events]
    assert len({id(e) for e in view.events}) == len(view.events)
    assert view.statuses == [
        ("outer", "start"),
        ("inner", "start"),
        ("inner", "end"),
        ("outer", "end"),
    ]
    assert next(e for e in view.events if e.kind == "log.line").stream == "stderr"


def test_headless_does_not_print_and_still_records_and_checks(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("terminal access")

    monkeypatch.setattr("builtins.print", forbidden)
    supervisor = CampaignSupervision(tmp_path, cadence=0.001)
    checks, releases = [], []
    resource = supervisor.resource(
        Resource(
            title="VM", acquire=lambda _: "vm", release=lambda _, v: releases.append(v)
        ),
        "vm",
        lambda _: checks.append(1),
    )
    workflow = Workflow("headless")
    workflow.add(Action(lambda _: time.sleep(0.02)), requires=(resource,))
    supervisor.run(workflow.run)
    assert checks and releases == ["vm"]
    assert json.loads((tmp_path / "result.json").read_text())["state"] == "completed"


@pytest.mark.parametrize("background", [False, True])
def test_display_failure_interrupts_wait_disables_view_and_preserves_cleanup(
    tmp_path, background
):
    class Broken(View):
        def emit(self, event):
            assert not getattr(self, "broken", False), "broken view reused"
            if event.kind == "log.line":
                self.broken = True
                raise OSError("selected display failed")
            super().emit(event)

    view = Broken()
    supervisor = CampaignSupervision(tmp_path, presentation=view, cadence=100)
    released, workers = [], []
    resource = Resource(
        title="VM", acquire=lambda _: "vm", release=lambda _, v: released.append(v)
    )
    workflow = Workflow("display-fault")

    def fail():
        time.sleep(0.02)
        workflow_log("trigger")

    def action(_):
        (tmp_path / "raw.jsonl").write_text('{"statusCode":429}\n')
        if background:
            worker = Thread(target=fail)
            workers.append(worker)
            worker.start()
        else:
            fail()
        time.sleep(2)
        pytest.fail("display failure did not stop load")

    workflow.add(Action(action), requires=(resource,))
    start = time.monotonic()
    with pytest.raises(OSError, match="selected display failed"):
        supervisor.run(workflow.run)
    for worker in workers:
        worker.join()
    assert time.monotonic() - start < 1
    assert released == ["vm"]
    assert json.loads((tmp_path / "result.json").read_text())["state"] == "failed"
    assert (tmp_path / "raw.jsonl").read_text() == '{"statusCode":429}\n'
    assert "Release VM" in (tmp_path / "supervision.jsonl").read_text()


def test_standard_sink_adapter_preserves_skipped_resource_events(tmp_path):
    from sonata_engine.workflow.events import WorkflowSink

    class Standard:
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

        @contextmanager
        def status(self, label):
            yield

    sink: WorkflowSink = Standard()
    resource = Resource(
        title="Retained fixture",
        acquire=lambda _: "value",
        release=lambda *_: pytest.fail("retained resource released"),
    )
    workflow = Workflow("retained", keep=True)
    workflow.add(Action(lambda _: None), requires=(resource,))
    with bind_workflow_sink(sink):
        CampaignSupervision(tmp_path).run(workflow.run)
    assert any(e.kind == "task.skipped" for e in sink.events)
    rows = [
        json.loads(s) for s in (tmp_path / "supervision.jsonl").read_text().splitlines()
    ]
    assert sum(r["kind"] == "task.skipped" for r in rows) == 1


@pytest.mark.parametrize("kind", ["task.started", "task.passed"])
def test_first_view_failure_during_release_does_not_skip_cleanup(tmp_path, kind):
    class Broken(View):
        def emit(self, event):
            if event.kind == kind and event.title == "Release VM":
                raise OSError("release display failed")
            super().emit(event)

    released = []
    resource = Resource(
        title="VM", acquire=lambda _: "vm", release=lambda _, v: released.append(v)
    )
    workflow = Workflow("release-display")
    workflow.add(Action(lambda _: None), requires=(resource,))
    with pytest.raises(OSError, match="release display failed"):
        CampaignSupervision(tmp_path, presentation=Broken()).run(workflow.run)
    assert released == ["vm"]


def test_console_reuses_standard_task_duration_and_error_policy():
    from sonata_engine.workflow.event_builders import build_log_event, build_task_event

    from nanolab.cli.progress import ConsoleProgressSink
    from nanolab.one_shot.views import ConsoleCampaignPresentation

    expected, actual = [], []
    standard = ConsoleProgressSink(write=expected.append, clock=iter([10, 12]).__next__)
    view = ConsoleCampaignPresentation(
        write=actual.append, clock=iter([10, 12]).__next__
    )
    events = [
        build_task_event(kind="task.started", task_id="a", title="Acquire"),
        build_log_event(line="hidden raw"),
        build_task_event(
            kind="task.failed", task_id="a", title="Acquire", detail="disk full"
        ),
    ]
    for event in events:
        standard.emit(event)
        view.emit(event)
    assert actual == expected
    assert "2.0s" in actual[-1] and "disk full" in actual[-1]
    view.campaign_event(
        {
            "kind": "heartbeat",
            "observation": {"mode": "oracle", "phase": "campaign", "epoch": 42},
            "future": [1, 2],
        }
    )
    assert "42" in actual[-1] and not actual[-1].startswith("{")


def test_tui_uses_existing_hierarchy_logs_and_actual_campaign_fields():
    from io import StringIO

    from rich.console import Console
    from sonata_engine.workflow.event_builders import build_log_event, build_task_event

    from nanolab.one_shot.views import open_presentation

    stream = StringIO()
    console = Console(file=stream, width=140)
    with open_presentation("tui", console=console) as view:
        view.emit(
            build_task_event(kind="task.started", task_id="parent", title="Parent")
        )
        view.emit(
            build_task_event(
                kind="task.started",
                task_id="child",
                parent_task_id="parent",
                title="Child",
            )
        )
        view.emit(build_log_event(line="subprocess output"))
        view.campaign_event(
            {
                "kind": "heartbeat",
                "observation": {"mode": "oracle", "phase": "campaign", "epoch": 42},
                "future": {"optional": 1},
            }
        )
        assert len(view.dashboard.steps) == 1
        assert view.dashboard.steps[0].children[0].label == "Child"
        assert "subprocess output" in view.dashboard.log_lines
        assert "epoch=42" in " ".join(view.dashboard.summary_lines)
    assert "oracle" in stream.getvalue() and "Parent" in stream.getvalue()


@pytest.mark.parametrize("boundary", ["open", "refresh", "start", "stop", "close"])
@pytest.mark.parametrize("prior", [None, "process", signal.SIGINT, signal.SIGTERM])
def test_tui_lifecycle_failures_have_failed_receipts_and_owned_archive(
    tmp_path, monkeypatch, boundary, prior
):
    from io import StringIO

    from rich.console import Console

    from nanolab.one_shot.report import render_report
    from nanolab.one_shot.views import open_presentation
    from nanolab.tui.workflow_controller import Live, WorkflowKeyListener
    from tests.one_shot.test_report import manifest

    raw, archive = tmp_path / "raw", tmp_path / "archive"
    raw.mkdir()
    console = Console(file=StringIO(), width=120)
    triggered = False
    started = False
    cls, name = {
        "open": (Console, "clear"),
        "refresh": (Live, "update"),
        "start": (WorkflowKeyListener, "start"),
        "stop": (WorkflowKeyListener, "stop"),
        "close": (Live, "stop"),
    }[boundary]
    original = getattr(cls, name)

    def fault(self, *args, **kwargs):
        nonlocal triggered
        result = original(self, *args, **kwargs)
        if not triggered and (boundary != "refresh" or started):
            triggered = True
            raise OSError("display " + boundary)
        return result

    monkeypatch.setattr(cls, name, fault)
    supervisor = CampaignSupervision(
        raw,
        archive=archive,
        presentation_factory=lambda fail: open_presentation(
            "tui", console=console, on_failure=fail
        ),
    )
    released = []
    resource = Resource(
        title="VM", acquire=lambda _: "vm", release=lambda _, v: released.append(v)
    )
    workflow = Workflow("lifecycle")

    def action(_):
        nonlocal started
        started = True
        (raw / "raw.jsonl").write_text('{"statusCode":503}\n')
        if prior == "process":
            raise ValueError("generator failed")
        if prior is not None:
            os.kill(os.getpid(), prior)
        workflow_log("refresh after acquisition")

    workflow.add(Action(action), requires=(resource,))
    render_report(manifest(), None, raw / "report.html")
    with pytest.raises((ValueError, RuntimeError, OSError)) as caught:
        supervisor.run(workflow.run)
    acquired = boundary not in {"open", "start"}
    assert released == (["vm"] if acquired else [])
    expected = (
        ("generator failed" if prior == "process" else f"signal {prior}")
        if acquired and prior
        else "display " + boundary
    )
    assert expected in str(caught.value)
    for directory in (raw, archive):
        for name in ("status.json", "result.json"):
            receipt = json.loads((directory / name).read_text())
            assert receipt["state"] == "failed"
            assert expected in receipt["cause"]
            assert receipt["exitCode"] == (
                128 + prior if acquired and isinstance(prior, int) else 1
            )
        assert (
            json.loads((directory / "report.json").read_text())["operationalOutcome"][
                "state"
            ]
            == "failed"
        )
    if acquired:
        assert (archive / "raw.jsonl").read_bytes() == (raw / "raw.jsonl").read_bytes()


def test_tui_background_refresh_fault_wakes_wait_and_does_not_redraw_broken_view(
    tmp_path, monkeypatch
):
    from io import StringIO

    from rich.console import Console
    from rich.live import Live

    from nanolab.one_shot.views import open_presentation

    trigger, attempts, released, workers = [], [], [], []
    refresh = Live.refresh

    def fail_refresh(self):
        if trigger:
            attempts.append(1)
            raise OSError("background refresh failed")
        return refresh(self)

    monkeypatch.setattr(Live, "refresh", fail_refresh)
    supervisor = CampaignSupervision(
        tmp_path,
        cadence=100,
        presentation_factory=lambda fail: open_presentation(
            "tui", console=Console(file=StringIO()), on_failure=fail
        ),
    )
    resource = Resource(
        title="VM", acquire=lambda _: "vm", release=lambda _, v: released.append(v)
    )
    workflow = Workflow("refresh-background")

    def action(_):
        def worker():
            time.sleep(0.02)
            trigger.append(True)
            workflow_log("background")

        thread = Thread(target=worker)
        workers.append(thread)
        thread.start()
        time.sleep(2)
        pytest.fail("renderer did not interrupt wait")

    workflow.add(Action(action), requires=(resource,))
    with pytest.raises(OSError, match="background refresh failed"):
        supervisor.run(workflow.run)
    for thread in workers:
        thread.join()
    assert released == ["vm"]
    assert attempts == [1]


def test_progress_helper_records_once_in_active_campaign_and_standalone(
    tmp_path, capsys
):
    from nanolab.tasks.one_shot.observation import event

    view = View()
    supervisor = CampaignSupervision(tmp_path, presentation=view)
    supervisor.run(lambda: event(tmp_path, "warmup", epoch=42))
    event(tmp_path, "standalone", epoch=43)
    rows = [
        json.loads(s) for s in (tmp_path / "progress.jsonl").read_text().splitlines()
    ]
    assert [(r["stage"], r["epoch"]) for r in rows] == [
        ("warmup", 42),
        ("standalone", 43),
    ]
    assert sum(row.get("stage") == "warmup" for row in view.rows) == 1
    assert not any(row.get("stage") == "standalone" for row in view.rows)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("channel", ["stage", "event"])
def test_background_recording_failure_interrupts_active_wait(
    tmp_path, monkeypatch, channel
):
    from pathlib import Path

    from nanolab.tasks.one_shot.observation import event

    supervisor = CampaignSupervision(tmp_path, cadence=100)
    released, workers, failures = [], [], []
    original = Path.open
    failed = False

    def write_failure(path, *args, **kwargs):
        nonlocal failed
        name = "progress.jsonl" if channel == "stage" else "supervision.jsonl"
        if path.name == name and workers and not failed:
            failed = True
            raise OSError("recording unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", write_failure)
    resource = Resource(
        title="VM", acquire=lambda _: "vm", release=lambda _, v: released.append(v)
    )
    workflow = Workflow("recorder-worker")

    def action(_):
        def worker():
            time.sleep(0.02)
            try:
                if channel == "stage":
                    event(tmp_path, "observing")
                else:
                    workflow_log("worker row")
            except Exception as error:
                failures.append(error)

        thread = Thread(target=worker)
        workers.append(thread)
        thread.start()
        time.sleep(2)
        pytest.fail("recorder fault did not interrupt wait")

    workflow.add(Action(action), requires=(resource,))
    start = time.monotonic()
    with pytest.raises(OSError, match="recording unavailable"):
        supervisor.run(workflow.run)
    for thread in workers:
        thread.join()
    assert time.monotonic() - start < 1
    assert released == ["vm"]
    assert json.loads((tmp_path / "result.json").read_text())["state"] == "failed"


@pytest.mark.parametrize(
    "failure", [None, "process", "recording", signal.SIGINT, signal.SIGTERM]
)
def test_operational_results_and_event_order_agree_across_views(
    tmp_path, monkeypatch, failure
):
    from io import StringIO
    from pathlib import Path

    from rich.console import Console

    from nanolab.one_shot.report import render_report
    from nanolab.one_shot.views import open_presentation, select_presentation
    from nanolab.tasks.one_shot.observation import event
    from tests.one_shot.test_report import manifest

    results = []
    for mode in ("console", "tui", "json", "none"):
        raw, archive = tmp_path / mode / "raw", tmp_path / mode / "archive"
        raw.mkdir(parents=True)
        factory = (
            (
                lambda fail: open_presentation(
                    "tui", console=Console(file=StringIO(), width=120), on_failure=fail
                )
            )
            if mode == "tui"
            else (lambda fail, mode=mode: select_presentation(mode, on_failure=fail))
        )
        supervisor = CampaignSupervision(
            raw, archive=archive, cadence=0.005, presentation_factory=factory
        )
        released = []
        resources = []
        for name in ("VM", "service", "generator", "collector"):
            resources.append(
                supervisor.resource(
                    Resource(
                        title=name,
                        acquire=lambda _, name=name: name,
                        release=lambda _, v, released=released: released.append(v),
                        requires=tuple(resources[-1:]),
                    ),
                    name,
                    lambda _: None,
                )
            )
        workflow = Workflow("equivalence")
        original = Path.open
        blocked = []

        def write_failure(
            path, *args, raw=raw, blocked=blocked, original=original, **kwargs
        ):
            if (
                failure == "recording"
                and path == raw / "progress.jsonl"
                and not blocked
            ):
                blocked.append(1)
                raise OSError("recording unavailable")
            return original(path, *args, **kwargs)

        def action(_, raw=raw):
            (raw / "requests.jsonl").write_text(
                '{"statusCode":429}\n{"statusCode":503}\n'
            )
            render_report(manifest(), None, raw / "report.html")
            with subtask(task_id="nested", title="Nested stage"), status("waiting"):
                worker = Thread(target=lambda: workflow_log("sample", stream="stderr"))
                worker.start()
                worker.join()
                event(raw, "measurement", epoch=42)
                if failure == "process":
                    raise RuntimeError("generator stopped unexpectedly: exit 7")
                if isinstance(failure, int):
                    os.kill(os.getpid(), failure)

        workflow.add(Action(action), requires=tuple(resources))
        with monkeypatch.context() as context:
            context.setattr(Path, "open", write_failure)
            if failure is None:
                supervisor.run(workflow.run)
            else:
                with pytest.raises((RuntimeError, OSError)):
                    supervisor.run(workflow.run)
        assert released == ["collector", "generator", "service", "VM"]
        result = json.loads((raw / "result.json").read_text())
        assert result["state"] == ("failed" if failure else "completed")
        assert result["exitCode"] == (
            128 + failure if isinstance(failure, int) else int(failure is not None)
        )
        assert result["archiveVerified"] is True
        supervisor._verify_archive()
        for directory in (raw, archive):
            assert (
                directory / "requests.jsonl"
            ).read_text() == '{"statusCode":429}\n{"statusCode":503}\n'
            assert (
                json.loads((directory / "report.json").read_text())[
                    "operationalOutcome"
                ]["state"]
                == result["state"]
            )
        rows = [
            json.loads(s) for s in (raw / "supervision.jsonl").read_text().splitlines()
        ]
        events = [
            tuple(
                row.get(key)
                for key in (
                    "kind",
                    "task_id",
                    "parent_task_id",
                    "title",
                    "detail",
                    "line",
                    "stream",
                )
            )
            for row in rows
            if row["kind"].startswith("task.") or row["kind"] == "log.line"
        ]
        results.append((events, result["state"], result["cause"], result["exitCode"]))
    assert results.count(results[0]) == 4


@pytest.mark.parametrize("mode", ["console", "tui", "json"])
def test_selected_real_view_output_fault_stops_generator(tmp_path, monkeypatch, mode):
    from io import StringIO

    from rich.console import Console
    from rich.live import Live

    from nanolab.one_shot.views import open_presentation, select_presentation
    from nanolab.tasks.one_shot.observation import event

    active, released = [], []
    original_print, original_refresh = print, Live.refresh

    def broken_print(*args, **kwargs):
        if active:
            raise BrokenPipeError("selected output unavailable")
        return original_print(*args, **kwargs)

    def broken_refresh(self):
        if active:
            raise BrokenPipeError("selected output unavailable")
        return original_refresh(self)

    monkeypatch.setattr("builtins.print", broken_print)
    monkeypatch.setattr(Live, "refresh", broken_refresh)
    factory = (
        (
            lambda fail: open_presentation(
                "tui", console=Console(file=StringIO()), on_failure=fail
            )
        )
        if mode == "tui"
        else (lambda fail: select_presentation(mode, on_failure=fail))
    )
    supervisor = CampaignSupervision(tmp_path, presentation_factory=factory)
    resource = Resource(
        title="generator",
        acquire=lambda _: "generator",
        release=lambda _, v: released.append(v),
    )
    workflow = Workflow("broken-output")

    def action(_):
        active.append(True)
        event(tmp_path, "generating")
        pytest.fail("generator continued after output failed")

    workflow.add(Action(action), requires=(resource,))
    with pytest.raises(BrokenPipeError, match="selected output unavailable"):
        supervisor.run(workflow.run)
    assert released == ["generator"]
    assert json.loads((tmp_path / "result.json").read_text())["exitCode"] == 1


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_during_tui_disposal_without_archive_is_persisted(
    tmp_path, monkeypatch, signum
):
    from io import StringIO

    from rich.console import Console

    from nanolab.one_shot.views import open_presentation
    from nanolab.tui.workflow import WorkflowKeyListener

    stop = WorkflowKeyListener.stop

    def interrupt_close(self):
        stop(self)
        os.kill(os.getpid(), signum)

    monkeypatch.setattr(WorkflowKeyListener, "stop", interrupt_close)
    supervisor = CampaignSupervision(
        tmp_path,
        presentation_factory=lambda fail: open_presentation(
            "tui", console=Console(file=StringIO()), on_failure=fail
        ),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        supervisor.run(lambda: None)
    assert (
        json.loads((tmp_path / "result.json").read_text())["exitCode"] == 128 + signum
    )


def test_status_disposal_failure_preserves_body_error(tmp_path):
    class BrokenStatus(View):
        @contextmanager
        def status(self, label):
            try:
                yield
            finally:
                raise OSError("status renderer failed")

    supervisor = CampaignSupervision(tmp_path, presentation=BrokenStatus())
    workflow = Workflow("status-failure")

    def action(_):
        with status("waiting"):
            raise ValueError("original process failure")

    workflow.add(Action(action))
    with pytest.raises(ValueError, match="original process failure") as caught:
        supervisor.run(workflow.run)
    assert "status renderer failed" in str(caught.value.__notes__)
    assert (
        json.loads((tmp_path / "result.json").read_text())["cause"]
        == "original process failure"
    )
