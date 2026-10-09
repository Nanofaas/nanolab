from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest


@contextmanager
def capture(**overrides):
    from nanolab.assets.diagnostics.artifact_contract import create_server

    settings = {
        "message_bytes": 2048,
        "capture_records": 10,
        "capture_bytes": 20000,
        "request_seconds": 0.2,
        **overrides,
    }
    server = create_server(settings, host="127.0.0.1", port=0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def request(base, path, value=None, *, raw=None):
    body = (
        raw
        if raw is not None
        else json.dumps(value).encode()
        if value is not None
        else None
    )
    req = urllib.request.Request(
        base + path, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=1) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def register(base, execution_id="one", expected=1):
    assert (
        request(
            base,
            "/_nanolab/register",
            {"executionId": execution_id, "expectedCallbacks": expected},
        )[0]
        == 200
    )


def snapshot(base):
    status, raw = request(base, "/_nanolab/records")
    assert status == 200
    return json.loads(raw)


def test_callback_id_comes_from_path():
    with capture() as base:
        register(base)
        assert (
            request(
                base, "/v1/executions/one:complete", {"success": True, "output": {}}
            )[0]
            == 200
        )
        observed = snapshot(base)
        assert observed["records"][0]["executionId"] == "one"
        assert observed["records"][0]["method"] == "POST"
        assert (
            request(base, "/_nanolab/body/1")[1] == b'{"success": true, "output": {}}'
        )
        assert observed["violations"] == []


def test_ack_does_not_wait_for_inspection():
    with capture() as base:
        register(base)
        started = time.monotonic()
        assert request(base, "/v1/executions/one:complete", {"success": True})[0] == 200
        assert time.monotonic() - started < 1


def test_duplicate_and_unknown_ids_retained():
    with capture() as base:
        register(base)
        for path in ("one", "one", "other"):
            request(base, f"/v1/executions/{path}:complete", {"success": True})
        observed = snapshot(base)
        assert len(observed["records"]) == 3
        assert "duplicate callback: one" in observed["violations"]
        assert "unknown execution: other" in observed["violations"]


def test_duplicate_keys_and_unknown_routes_fail():
    with capture() as base:
        register(base)
        assert (
            request(
                base,
                "/v1/executions/one:complete",
                raw=b'{"success":true,"success":false}',
            )[0]
            == 400
        )
        assert request(base, "/unexpected", {})[0] == 400
        assert len(snapshot(base)["violations"]) == 2


def test_warm_registration_rejects_callback():
    with capture() as base:
        register(base, expected=0)
        request(base, "/v1/executions/one:complete", {"success": True})
        assert "unexpected callback: one" in snapshot(base)["violations"]


def test_slow_body_hits_request_deadline():
    with capture() as base:
        register(base)
        with socket.create_connection(
            ("127.0.0.1", int(base.rsplit(":", 1)[1]))
        ) as sock:
            sock.sendall(
                b"POST /v1/executions/one:complete HTTP/1.1\r\n"
                b"Host: capture\r\nContent-Length: 20\r\n\r\n{"
            )
            sock.settimeout(1)
            assert b"408" in sock.recv(4096)
        assert snapshot(base)["violations"]


@pytest.mark.parametrize(
    "settings", [{"message_bytes": 512}, {"capture_records": 1}, {"capture_bytes": 2}]
)
def test_message_and_capture_bounds_fail(settings):
    with capture(**settings) as base:
        register(base)
        assert (
            request(
                base,
                "/v1/executions/one:complete",
                {"success": True, "output": "x" * 600},
            )[0]
            != 200
        )
        assert snapshot(base)["violations"]


def test_late_delivery_is_in_final_snapshot():
    with capture() as base:
        register(base)
        request(base, "/v1/executions/one:complete", {"success": True})
        assert len(snapshot(base)["records"]) == 1
        request(base, "/v1/executions/one:complete", {"success": True})
        assert "duplicate callback: one" in snapshot(base)["violations"]


def test_pending_body_visible_for_final_audit():
    with capture(request_seconds=1) as base:
        register(base)
        with socket.create_connection(
            ("127.0.0.1", int(base.rsplit(":", 1)[1]))
        ) as sock:
            sock.sendall(
                b"POST /v1/executions/one:complete HTTP/1.1\r\n"
                b"Host: capture\r\nContent-Length: 20\r\n\r\n{"
            )
            deadline = time.monotonic() + 0.5
            while True:
                observed = snapshot(base)
                if observed["activeCallbacks"] or time.monotonic() >= deadline:
                    break
            assert observed["activeCallbacks"] == 1


def test_unsupported_method_retains_violation():
    with capture() as base:
        register(base)
        req = urllib.request.Request(
            base + "/v1/executions/one:complete", data=b'{"success":true}', method="PUT"
        )
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=1)
        observed = snapshot(base)
        assert observed["violations"]
        assert observed["records"][0]["method"] == "PUT"
        assert request(base, "/_nanolab/body/1")[1] == b'{"success":true}'


def test_pending_headers_and_absolute_deadline_visible():
    with capture(request_seconds=0.2) as base:
        register(base)
        with socket.create_connection(
            ("127.0.0.1", int(base.rsplit(":", 1)[1]))
        ) as sock:
            sock.sendall(b"POST /v1/executions/one:complete HTTP/1.1\r\nHost: ")
            deadline = time.monotonic() + 0.15
            while True:
                observed = snapshot(base)
                if observed.get("activeRequests") or time.monotonic() >= deadline:
                    break
            assert observed.get("activeRequests") == 1
            time.sleep(0.25)
            assert snapshot(base)["violations"]


def test_slow_body_preserves_received_prefix():
    with capture() as base:
        register(base)
        with socket.create_connection(
            ("127.0.0.1", int(base.rsplit(":", 1)[1]))
        ) as sock:
            sock.sendall(
                b"POST /v1/executions/one:complete HTTP/1.1\r\n"
                b'Host: capture\r\nContent-Length: 20\r\n\r\n{"x":'
            )
            sock.settimeout(1)
            sock.recv(4096)
        assert snapshot(base)["violations"]
        assert request(base, "/_nanolab/body/1")[1] == b'{"x":'
