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
