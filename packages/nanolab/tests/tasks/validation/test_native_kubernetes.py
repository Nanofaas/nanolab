"""Exercise native API checks through curl against an independent HTTP server."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.local import LocalCommandTaskExecutor

from nanolab.tasks.platform import PlatformFunction


@pytest.fixture
def native_api_server():
    state = {"fault": None, "calls": [], "registered": True}
    function = {
        "name": "fn",
        "image": "image:qualified",
        "requestedExecutionMode": "DEPLOYMENT",
        "effectiveExecutionMode": "DEPLOYMENT",
        "deploymentBackend": "k8s",
        "concurrency": 1,
        "timeoutMs": 5000,
    }

    class Handler(BaseHTTPRequestHandler):
        def handle_api(self):
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length)) if length else None
            state["calls"].append((self.command, self.path, data))
            status, body = 200, dict(function)
            key = self.command + " " + self.path
            if self.command == "DELETE":
                status, body = 204, None
                state["registered"] = False
            elif self.command == "POST" and self.path == "/v1/functions":
                status = 201
                state["registered"] = True
            elif self.path.endswith("/replicas"):
                body = (
                    {"function": "fn", "replicas": 1}
                    if self.command == "PUT"
                    else {
                        "function": "fn",
                        "desiredReplicas": 1,
                        "readyReplicas": 1,
                        "pods": [],
                    }
                )
            elif self.path.endswith(":invoke"):
                body = {
                    "status": "success",
                    "statusCode": 200,
                    "output": {"wordCount": 3},
                }
            if state["fault"] == key:
                body = {
                    "name": "other",
                    "function": "other",
                    "status": "timeout",
                    "desiredReplicas": 1,
                    "readyReplicas": 1,
                }
                if self.command == "DELETE":
                    status = 200
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    for method in ("GET", "POST", "PATCH", "PUT", "DELETE"):
        setattr(Handler, "do_" + method, Handler.handle_api)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "GET /v1/functions/fn",
        "POST /v1/functions",
        "PATCH /v1/functions/fn",
        "GET /v1/functions/fn/replicas",
        "PUT /v1/functions/fn/replicas",
        "POST /v1/functions/fn:invoke",
        "DELETE /v1/functions/fn",
    ],
)
def test_native_api_validates_bodies_and_strict_delete(
    native_api_server, tmp_path, fault
):
    from nanolab.tasks.validation.native_kubernetes import check_native_api

    url, state = native_api_server
    state["fault"] = fault
    fn = PlatformFunction(
        name="fn",
        image="image:qualified",
        payload='{"input":{"text":"one two one"}}',
        build_argv=("true",),
    )

    def run():
        check_native_api(
            TaskInputs.empty(),
            function=fn,
            endpoint=url,
            executor=LocalCommandTaskExecutor(),
            role="host",
            cwd=tmp_path,
            evidence_file=tmp_path / "api.jsonl",
        )

    if fault:
        with pytest.raises(RuntimeError):
            run()
    else:
        run()
        assert state["registered"] is True
        assert (
            sum(
                method == "POST" and path == "/v1/functions"
                for method, path, _ in state["calls"]
            )
            == 2
        )
        assert any(
            method == "PATCH" and data == {"concurrency": 1, "timeoutMs": 5000}
            for method, _, data in state["calls"]
        )
        assert (tmp_path / "api.jsonl").read_text()
