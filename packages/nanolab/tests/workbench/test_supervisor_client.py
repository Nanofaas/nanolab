"""SupervisorClient against a stub of the workbench supervisor API."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nanolab.workbench.supervisor import SupervisorClient

STATUS = {"state": "healthy", "targets": {}, "anomalies": [], "checkedAt": None}


@pytest.fixture
def stub():
    seen: list[tuple[str, str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _reply(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            seen.append(("GET", self.path, None))
            self._reply(200, json.dumps(STATUS).encode())

        def do_PUT(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(("PUT", self.path, json.loads(raw)))
            self._reply(204)

        def do_DELETE(self):
            seen.append(("DELETE", self.path, None))
            self._reply(204)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()
    server.server_close()


def test_put_delete_status_speak_the_supervisor_api(stub):
    url, seen = stub
    client = SupervisorClient(url + "/", timeout_seconds=2)
    client.put("n1", "scrape_up", {"job": "node"}, 30)
    client.put("n2", "http_ok", {"url": "http://x"}, 0, grace_seconds=5)
    client.delete("n1")
    assert client.status() == STATUS
    assert seen == [
        (
            "PUT",
            "/targets/n1",
            {
                "kind": "scrape_up",
                "params": {"job": "node"},
                "toleratedSeconds": 30,
            },
        ),
        (
            "PUT",
            "/targets/n2",
            {
                "kind": "http_ok",
                "params": {"url": "http://x"},
                "toleratedSeconds": 0,
                "graceSeconds": 5,
            },
        ),
        ("DELETE", "/targets/n1", None),
        ("GET", "/status", None),
    ]


def test_unreachable_supervisor_raises_oserror():
    client = SupervisorClient("http://127.0.0.1:1", timeout_seconds=0.5)
    with pytest.raises(OSError, match=r"."):
        client.status()
