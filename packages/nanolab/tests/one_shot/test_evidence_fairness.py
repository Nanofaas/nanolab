"""Exercise actual bounded collection across repeated passes without live VMs."""

import json
from contextlib import nullcontext
from threading import Lock
from types import SimpleNamespace

import httpx
import pytest

from nanolab.tasks.one_shot import experiment


@pytest.mark.parametrize("late_arrivals", [False, True])
def test_unrecoverable_prefix_does_not_starve_new_available_proofs(
    tmp_path, monkeypatch, late_arrivals
):
    now = [0.0]
    lock = Lock()
    queries = []
    rows = [
        {
            "event": "result",
            "phase": "campaign",
            "originalId": identity,
            "executionId": identity,
            "terminalExecutionId": identity,
            "function": "work",
        }
        for identity in [
            *(f"old-{i}" for i in range(64)),
            *(f"new-{i}" for i in range(16)),
        ]
    ]
    (tmp_path / "generator.jsonl").write_text(
        "".join(
            json.dumps({"msg": json.dumps(row)}) + "\n"
            for row in (rows[:64] if late_arrivals else rows)
        )
    )

    class DormantThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    class Passes:
        count = 0

        def wait(self, _):
            self.count += 1
            if late_arrivals and self.count == 5:
                with (tmp_path / "generator.jsonl").open("a") as stream:
                    stream.write(
                        "".join(
                            json.dumps({"msg": json.dumps(row)}) + "\n"
                            for row in rows[64:]
                        )
                    )
            return self.count > 24

        def is_set(self):
            return False

    def respond(request):
        if "/runtime/executions/" in request.url.path:
            identity = request.url.path.rsplit("/", 1)[1]
            with lock:
                now[0] += 5
                queries.append(identity)
            if identity.startswith("old"):
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "executionId": identity,
                    "incarnation": "replica",
                    "dispatchAttempt": "1",
                    "state": "RELEASED",
                    "handlerStarted": True,
                },
            )
        if request.url.path == "/runtime/status":
            return httpx.Response(200, json={})
        return httpx.Response(200, text="")

    run = SimpleNamespace(
        run_dir=tmp_path,
        topology=SimpleNamespace(
            resources=SimpleNamespace(nodes={"cloud": object()}),
            function_settings={"work": SimpleNamespace(max_replicas=1)},
            endpoints={"cloud": "endpoint"},
        ),
    )
    inputs = SimpleNamespace(resource=lambda _: "http://gateway:8080")
    monkeypatch.setattr(experiment, "Thread", DormantThread)
    monkeypatch.setattr(experiment.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        experiment, "runtime_endpoint", lambda *_args, **_kw: ("http://sdk", {})
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(experiment.httpx, "Client", lambda **_: nullcontext(client))
        collector = experiment.LiveEvidenceCollector(run, inputs)
        monkeypatch.setattr(collector, "stop", Passes())
        collector.collect_events = lambda: None
        collector.loop()
    collector.require_healthy()
    proofs = experiment.physical_attempts(tmp_path / "physical.jsonl")
    assert {row["originalId"] for row in proofs} == {f"new-{i}" for i in range(16)}
    assert len({identity for identity in queries if identity.startswith("old")}) == 64
    journal = [
        json.loads(line)
        for line in (tmp_path / "evidence-collection.jsonl").read_text().splitlines()
    ]
    assert journal[-1]["pendingOriginals"] == 64
    assert journal[-1]["provenOriginals"] == 16
    assert all(row["atUtc"] for row in journal)


def test_expired_collection_slice_returns_no_fictitious_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment.time, "monotonic", lambda: 10)
    assert experiment.collect_runtime_evidence(
        SimpleNamespace(run_dir=tmp_path), object(), [], deadline=9
    ) == ([], set())
    assert not list(tmp_path.iterdir())
