from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nanolab.tasks.one_shot.experiment import LiveEvidenceCollector


def test_live_collector_failure_is_visible_and_owned_thread_is_joined(
    tmp_path, monkeypatch
):
    called = Event()

    def collect(*_, **_kwargs):
        called.set()
        raise ValueError("SDK collection unavailable")

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.collect_runtime_evidence", collect
    )
    collector = LiveEvidenceCollector(SimpleNamespace(run_dir=tmp_path), object())
    assert called.wait(2)
    collector.close()
    with pytest.raises(RuntimeError, match="collector failed"):
        collector.require_healthy()
    assert not collector.thread.is_alive()
    collector.close()


def test_live_collector_unjoined_thread_is_not_reported_as_clean():
    collector = cast(Any, object.__new__(LiveEvidenceCollector))
    collector.stop = Event()
    collector.thread = SimpleNamespace(join=lambda **_: None, is_alive=lambda: True)
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        collector.close()


def test_live_event_pages_persist_before_later_error_and_resume_cursor(
    tmp_path, monkeypatch
):
    import json
    from contextlib import nullcontext
    from pathlib import Path

    import httpx

    from nanolab.config.one_shot import OperationalLimits
    from tests.one_shot.test_report import manifest

    model = manifest()
    model.parameters.update(
        observationOnly=True,
        logicalSchedule={
            "protocolVersion": 2,
            "sessionId": "s",
            "anchorMonotonicSeconds": 100,
            "hostBootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        },
    )
    (tmp_path / "manifest.json").write_text(model.model_dump_json())
    monkeypatch.setattr("nanolab.tasks.one_shot.experiment.time.monotonic", lambda: 100)
    collector = cast(Any, object.__new__(LiveEvidenceCollector))
    collector.stop = Event()
    collector.event_cursors = {}
    collector.run = SimpleNamespace(
        run_dir=tmp_path,
        settings=SimpleNamespace(operational_limits=OperationalLimits()),
        trace=SimpleNamespace(windows=[{}] * 9),
        topology=SimpleNamespace(
            neighbor_graph={"edge": []}, endpoints={"edge": "endpoint"}
        ),
    )
    collector.inputs = SimpleNamespace(resource=lambda _: "http://edge")
    calls = []

    def respond(request):
        calls.append(int(request.url.params["after"]))
        if len(calls) == 2:
            return httpx.Response(503)
        event = {
            "schemaVersion": 1,
            "nodeId": "edge",
            "incarnation": "i",
            "epoch": 0,
            "round": 0,
            "type": "START",
            "monotonicOffsetNanos": 0,
            "at": "2026-10-09T10:00:00Z",
            "status": "START",
            "censored": False,
            "correlationId": "c",
        }
        entries = (
            [
                {"cursor": i, "event": event, "operationalNanos": 0}
                for i in range(1, 101)
            ]
            if calls[-1] == 0
            else []
        )
        return httpx.Response(
            200,
            json={
                "schemaVersion": 1,
                "contentHash": "sha256:" + "a" * 64,
                "entries": entries,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.httpx.Client",
            lambda **_: nullcontext(http),
        )
        collector.collect_events()
        collector.collect_events()
    assert calls == [0, 100, 100]
    rows = [
        json.loads(line)
        for line in (tmp_path / "runtime-events.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 2
    assert len(rows[0]["page"]["entries"]) == 100
    assert rows[1]["measurementComplete"] is False
    assert rows[1]["after"] == 100
    assert collector.event_cursors["edge", 0] == 100
