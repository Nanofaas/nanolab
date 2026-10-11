"""Keep logical negotiation and physical activation distinct at the HTTP boundary."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from nanolab.one_shot.client import NanoFaasOneShotClient


def test_logical_configuration_prepare_activate_and_status_preserve_session() -> None:
    requests: list[httpx.Request] = []
    start = datetime(2020, 1, 1, tzinfo=UTC)
    settings = {"schemaVersion": 2, "sessionId": "session"}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={"revision": 1, "settings": settings})
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 2,
                    "sessionId": "session",
                    "state": "PREPARED",
                    "busy": False,
                    "clockHealthy": False,
                    "catalogGenerations": {"f": 1},
                    "peerEndpoints": [],
                    "localEndpoint": {},
                    "revision": 1,
                    "activePlan": {},
                    "automaticArmed": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "NEGOTIATED"
                if request.url.path.endswith("/prepare")
                else "PREPARED",
                "plan": None,
                "desiredReplicas": {"f": 1},
                "readyReplicas": {},
                "reason": "fixture",
                "preparedAt": start.isoformat(),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = NanoFaasOneShotClient("http://edge:8080", http=http)
        assert client.configure(settings, revision=0).settings == settings
        negotiated = client.prepare_logical_epoch(
            "session",
            0,
            starts_at=start,
            ends_at=start + timedelta(seconds=360),
            activation_delay_seconds=5,
        )
        assert negotiated.status == "NEGOTIATED"
        assert client.activate_epoch("session", 0).status == "PREPARED"
        assert client.status().session_id == "session"
    assert [request.url.path for request in requests] == [
        "/v1/admin/offload/one-shot/config",
        "/v1/admin/offload/one-shot/sessions/session/epochs/0/prepare",
        "/v1/admin/offload/one-shot/sessions/session/epochs/0/activate",
        "/v1/admin/offload/one-shot/status",
    ]
    import json

    assert json.loads(requests[1].content)["activationDelay"] == "PT5S"


@pytest.mark.parametrize("delay", [0, -1, float("nan"), float("inf"), 86401])
def test_invalid_logical_delay_fails_before_http(delay: float) -> None:
    requests: list[httpx.Request] = []
    start = datetime(2020, 1, 1, tzinfo=UTC)

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as http:
        client = NanoFaasOneShotClient("http://edge:8080", http=http)
        with pytest.raises(ValueError, match="delay"):
            client.prepare_logical_epoch(
                "session",
                0,
                starts_at=start,
                ends_at=start + timedelta(seconds=360),
                activation_delay_seconds=delay,
            )
    assert requests == []
