"""Valid degraded plans and missing diagnostic reads remain observable results."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from nanolab.one_shot.client import NanoFaasOneShotClient
from nanolab.tasks.one_shot.qualification import prepare_parallel_epoch
from nanolab.tasks.one_shot.verification import verify_epoch_flows


@pytest.mark.parametrize("ready", [0, 1])
@pytest.mark.parametrize("missing_metrics", [False, True])
def test_degraded_activation_is_usable_with_partial_readiness_and_missing_metrics(
    tmp_path, monkeypatch, ready, missing_metrics
):
    start = datetime(2020, 1, 1, tzinfo=UTC)
    now = [100.0]
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.qualification.time",
        SimpleNamespace(
            monotonic=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        ),
    )
    count = [0]

    def response(request):
        if request.url.path.endswith("prometheus"):
            if missing_metrics:
                return httpx.Response(503)
            return httpx.Response(
                200,
                text=f"nanofaas_oneshot_auction_seconds_count {count[0]}\n"
                f"nanofaas_oneshot_auction_seconds_sum {count[0]}\n",
            )
        if request.url.path.endswith("events"):
            return httpx.Response(503)
        preparing = request.url.path.endswith("prepare")
        count[0] = 1
        now[0] += 0.1
        return httpx.Response(
            200,
            json={
                "status": "NEGOTIATED" if preparing else "DEGRADED",
                "plan": None
                if preparing
                else {"functions": {"f": {"readyReplicas": ready}}},
                "desiredReplicas": {"f": 1},
                "readyReplicas": {"f": ready},
                "reason": "unconfirmed outbound goes to cloud",
                "preparedAt": start.isoformat(),
                "diagnostics": [],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        sample, evidence = prepare_parallel_epoch(
            {"edge": NanoFaasOneShotClient("http://edge", http=http)},
            epoch=0,
            starts_at=start,
            ends_at=start + timedelta(seconds=360),
            output=tmp_path / "epochs.jsonl",
            session_id="s",
            activation_at=110,
            clock=cast(Any, SimpleNamespace(require_healthy=lambda: None)),
            observation_only=True,
        )
    assert sample.censored is False
    assert sample.ready_seconds is not None
    row = evidence["nodes"]["edge"]
    assert row["executionUsable"] is True
    assert row["fullyPrepared"] is False
    assert row["measurementComplete"] is False
    assert row["result"]["status"] == "DEGRADED"


def test_unused_seller_reservation_is_recorded_but_unauthorized_outbound_fails():
    grant = {
        "id": "g",
        "buyerId": "a",
        "sellerId": "b",
        "quantity": 1,
        "readyConfirmed": True,
    }

    def function(**extra):
        return dict(
            forecastRate=0, localRate=0, cloudRate=0, inbound=[], outbound=[], **extra
        )

    a = function()
    b = function()
    b["inbound"] = [grant]
    plans = {
        node: {"flowQuantum": 1, "functions": {"f": fn}}
        for node, fn in [("a", a), ("b", b)]
    }
    rows = verify_epoch_flows(
        plans, neighbor_graph={"a": ["b"], "b": ["a"]}, observation_only=True
    )
    assert rows == [
        {"kind": "unusedReservation", "node": "b", "function": "f", "assignment": grant}
    ]
    with pytest.raises(ValueError, match="reciprocal"):
        verify_epoch_flows(plans)
    a.update(forecastRate=1, outbound=[grant])
    b["inbound"] = []
    with pytest.raises(ValueError, match="reciprocal"):
        verify_epoch_flows(plans, observation_only=True)


def test_missing_peer_does_not_hide_invalid_available_plan():
    assignment = {
        "id": "g",
        "buyerId": "a",
        "sellerId": "b",
        "quantity": 1,
        "readyConfirmed": True,
    }
    function = {
        "forecastRate": 1,
        "localRate": 0,
        "cloudRate": 0,
        "inbound": [],
        "outbound": [assignment],
    }
    plans = {"a": {"flowQuantum": 1, "functions": {"f": function}}}
    diagnostics = verify_epoch_flows(
        plans,
        neighbor_graph={"a": ["b"]},
        observation_only=True,
        unavailable_nodes={"b"},
    )
    assert diagnostics[0]["kind"] == "unobservedPeerConfirmation"
    function["localRate"] = 2
    with pytest.raises(ValueError, match="conserve"):
        verify_epoch_flows(
            plans,
            neighbor_graph={"a": ["b"]},
            observation_only=True,
            unavailable_nodes={"b"},
        )
    function["localRate"] = 0
    with pytest.raises(ValueError, match="topology"):
        verify_epoch_flows(
            plans,
            neighbor_graph={"a": []},
            observation_only=True,
            unavailable_nodes={"b"},
        )
    with pytest.raises(ValueError, match="topology"):
        verify_epoch_flows(plans, neighbor_graph={"a": ["b"]})
