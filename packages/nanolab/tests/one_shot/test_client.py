"""Exercise the API boundary without a live control plane."""

from __future__ import annotations

import json

import httpx
import pytest

from nanolab.one_shot.client import NanoFaasOneShotClient, OneShotConflictError
from nanolab.one_shot.contracts import contract_asset
from nanolab.one_shot.models import CalibrationProfile


def test_profile_upload_preserves_hash_and_revision_on_conflict() -> None:
    """Concurrent changes must be surfaced rather than overwritten."""
    data = json.loads(contract_asset("examples/synthetic-profile.json").read_text())
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(409, json={"error": "ONE_SHOT_CONFLICT"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = NanoFaasOneShotClient("http://edge:8080", http=http)
        with pytest.raises(OneShotConflictError):
            client.load_profile(CalibrationProfile.model_validate(data), revision=7)
    assert len(requests) == 1
    assert requests[0].headers["If-Match"] == "7"
    assert requests[0].headers["X-Content-SHA256"].startswith("sha256:")
    assert json.loads(requests[0].content) == data


def test_status_rejects_unsupported_contract_version() -> None:
    """Successful HTTP with incompatible data is still a failed operation."""
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"schemaVersion": 2})
            )
        ) as http,
        pytest.raises(ValueError, match="schemaVersion"),
    ):
        NanoFaasOneShotClient("http://edge:8080", http=http).status()


def test_transport_failure_is_distinct_from_api_conflict() -> None:
    """No blind retry is introduced for network failure."""

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(fail)) as http,
        pytest.raises(httpx.ConnectError, match="offline"),
    ):
        NanoFaasOneShotClient("http://edge:8080", http=http).status()


def test_profile_success_checks_exact_receipt_hash() -> None:
    data = json.loads(contract_asset("examples/synthetic-profile.json").read_text())

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "revision": 1,
                "contentHash": "sha256:" + "0" * 64,
                "profile": json.loads(request.content),
                "bytes": len(request.content),
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        pytest.raises(ValueError, match="receipt identity"),
    ):
        NanoFaasOneShotClient("http://edge:8080", http=http).load_profile(
            CalibrationProfile.model_validate(data), revision=0
        )


def test_events_reject_stalled_cursor() -> None:
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
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "schemaVersion": 1,
                "contentHash": "sha256:" + "a" * 64,
                "entries": [{"cursor": 1, "event": event, "operationalNanos": 0}],
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        pytest.raises(ValueError, match="cursor"),
    ):
        NanoFaasOneShotClient("http://edge:8080", http=http).epoch_events(0, limit=1)
    assert len(calls) == 2
    assert calls[1].url.params["after"] == "1"


def test_clock_client_can_bound_its_request_during_teardown():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"healthy": True})

    from datetime import UTC, datetime

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = NanoFaasOneShotClient("http://edge:8080", http=http, timeout_seconds=2)
        client.update_clock_health(offset_seconds=0.001, measured_at=datetime.now(UTC))
    assert requests[0].extensions["timeout"]["read"] == 2


def test_configuration_trace_and_drain_use_versioned_public_contracts():
    from datetime import UTC, datetime

    requests = []
    now = datetime.now(UTC).isoformat()

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/config"):
            return httpx.Response(
                200, json={"revision": 1, "settings": {"schemaVersion": 1}}
            )
        if request.url.path.endswith("/trace"):
            return httpx.Response(
                200,
                json={
                    "revision": 1,
                    "entryCount": 0,
                    "nodeId": "edge",
                    "producedAt": now,
                },
            )
        return httpx.Response(200, json=True)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = NanoFaasOneShotClient("http://edge", http=http)
        assert client.configure({"schemaVersion": 1}, revision=0).revision == 1
        assert (
            client.load_trace(
                {
                    "schemaVersion": 1,
                    "nodeId": "edge",
                    "revision": 1,
                    "provider": "oracle",
                    "producedAt": now,
                    "entries": [],
                },
                revision=0,
            ).entry_count
            == 0
        )
        assert client.drain_and_release()
    assert len(requests) == 3
    assert requests[0].headers["If-Match"] == "0"


def test_invalid_requests_are_refused_before_transport():
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: pytest.fail("invalid request reached transport")
        )
    ) as http:
        client = NanoFaasOneShotClient("http://edge", http=http)
        with pytest.raises(ValueError, match="configuration version"):
            client.configure({"schemaVersion": 2}, revision=0)
        with pytest.raises(ValueError, match="epoch window"):
            client.prepare_epoch(-1, starts_at=now, ends_at=now)
        with pytest.raises(ValueError, match="pagination"):
            client.epoch_events(-1)
        with pytest.raises(ValueError, match="finite"):
            NanoFaasOneShotClient(
                "http://edge", http=http, timeout_seconds=float("inf")
            )


def test_false_drain_and_invalid_receipt_are_distinct():
    for receipt in [False, {"released": True}]:
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda _, receipt=receipt: httpx.Response(200, json=receipt)
            )
        ) as http:
            client = NanoFaasOneShotClient("http://edge", http=http)
            if receipt is False:
                assert not client.drain_and_release()
            else:
                with pytest.raises(ValueError, match="drain receipt"):
                    client.drain_and_release()
