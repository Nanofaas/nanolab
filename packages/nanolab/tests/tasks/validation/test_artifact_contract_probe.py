from __future__ import annotations

import base64
import http.server
import threading
import time

import pytest

from tests.tasks.validation.test_artifact_contract_capture import capture, request


def test_probe_preserves_non_2xx_body():
    from nanolab.assets.diagnostics.artifact_contract import probe

    with capture() as base:
        result = probe(
            {"url": base + "/unknown", "method": "GET"},
            {"request_seconds": 1, "message_bytes": 2048},
        )
        assert result["status"] == 400
        assert base64.b64decode(result["bodyBase64"])


def test_probe_performs_actual_request():
    from nanolab.assets.diagnostics.artifact_contract import probe

    with capture() as base:
        result = probe(
            {"url": base + "/health", "method": "GET"},
            {"request_seconds": 1, "message_bytes": 2048},
        )
        assert result["status"] == 200
        assert request(base, "/_nanolab/records")[0] == 200


def test_probe_slow_response_deadline():
    from nanolab.assets.diagnostics.artifact_contract import probe

    class Slow(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            try:
                for _ in range(10):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.04)
            except BrokenPipeError:
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises((TimeoutError, ValueError), match="deadline"):
            probe(
                {"url": f"http://127.0.0.1:{server.server_port}"},
                {"request_seconds": 0.1, "message_bytes": 2048},
            )
        assert time.monotonic() - started < 0.25
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)
