"""Logical negotiation precedes the common activation boundary; waits are not work."""

import json
from datetime import UTC, datetime, timedelta
from threading import Lock
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from nanolab.one_shot.client import NanoFaasOneShotClient
from nanolab.tasks.one_shot.clock import ClockMonitor
from nanolab.tasks.one_shot.qualification import prepare_parallel_epoch


@pytest.mark.parametrize("failed_node", [None, "edge0"])
def test_parallel_logical_epoch_activates_after_all_negotiations_and_excludes_wait(
    tmp_path, monkeypatch, failed_node
):
    now = [100.0]
    lock = Lock()
    negotiated = set()
    calls = []
    counters = {}

    def monotonic():
        with lock:
            return now[0]

    def sleep(seconds):
        with lock:
            now[0] += seconds

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.qualification.time",
        SimpleNamespace(monotonic=monotonic, sleep=sleep),
    )
    start = datetime(2020, 1, 1, tzinfo=UTC)

    def respond(request):
        node = request.url.host
        if request.url.path == "/actuator/prometheus":
            return httpx.Response(
                200,
                text=(
                    f"nanofaas_oneshot_auction_seconds_count {counters.get(node, 0)}\n"
                    f"nanofaas_oneshot_auction_seconds_sum {counters.get(node, 0)}\n"
                    "nanofaas_oneshot_solver_seconds_sum 0\n"
                ),
            )
        if request.url.path.endswith("/events"):
            event = {
                "schemaVersion": 1,
                "nodeId": node,
                "incarnation": "run",
                "epoch": 0,
                "round": 0,
                "type": "START",
                "monotonicOffsetNanos": 0,
                "at": start.isoformat(),
                "status": "START",
                "censored": False,
                "correlationId": "session",
            }
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 1,
                    "contentHash": "sha256:" + "a" * 64,
                    "entries": [{"cursor": 1, "event": event, "operationalNanos": 0}],
                },
            )
        if request.url.path.endswith("/prepare"):
            with lock:
                now[0] += 1
                negotiated.add(node)
                counters[node] = 1
            calls.append((node, "prepare", json.loads(request.content)))
            if node == failed_node:
                return httpx.Response(503, json={"error": "negotiation failed"})
            status = "NEGOTIATED"
        elif request.url.path.endswith("/activate"):
            assert negotiated == {"edge0", "edge1"}
            assert monotonic() >= 110
            with lock:
                now[0] += 1
            calls.append((node, "activate", {}))
            status = "PREPARED"
        else:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "status": status,
                "plan": None,
                "desiredReplicas": {"f": 1},
                "readyReplicas": {"f": 1},
                "reason": "fixture",
                "preparedAt": start.isoformat(),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        clients = {
            node: NanoFaasOneShotClient("http://" + node, http=http)
            for node in ("edge0", "edge1")
        }
        sample, evidence = prepare_parallel_epoch(
            clients,
            epoch=0,
            starts_at=start,
            ends_at=start + timedelta(seconds=360),
            output=tmp_path / "epochs.jsonl",
            session_id="session",
            activation_at=110,
            clock=cast(ClockMonitor, SimpleNamespace(require_healthy=lambda: None)),
        )
    assert sample.censored == (failed_node is not None)
    assert sample.auction_seconds == (None if failed_node else 4)
    assert sample.ready_seconds == (None if failed_node else 4)
    assert evidence["globalReadyWallSeconds"] == (11 if failed_node else 12)
    assert evidence["protocolVersion"] == 2
    assert [phase for _, phase, _ in calls] == ["prepare", "prepare"] + ["activate"] * (
        1 if failed_node else 2
    )
    assert (tmp_path / "epochs.jsonl").exists()
    raw = [
        json.loads(line)
        for line in (tmp_path / "epochs-negotiations.jsonl").read_text().splitlines()
    ]
    assert len(raw) == 2
    if failed_node:
        failure = next(row for row in raw if row["node"] == failed_node)
        assert failure["statusCode"] == 503
        assert "negotiation failed" in failure["responseBody"]
