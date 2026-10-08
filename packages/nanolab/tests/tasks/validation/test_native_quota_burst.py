"""Qualify quota responses using an independent concurrent HTTP fixture."""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

import pytest


@pytest.fixture
def quota_server():
    state = {"active": 0, "mode": "quota"}
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            with lock:
                overlap = state["active"] > 0
                state["active"] += 1
            try:
                status = 200
                body = {"status": "success", "statusCode": 200, "output": {}}
                if overlap and state["mode"] != "none":
                    status = 429
                    body = {"error": "invocation_quota_exceeded"}
                    if state["mode"] == "queue":
                        body = {"error": "queue_full"}
                    elif state["mode"] == "malformed":
                        body = ["invocation_quota_exceeded"]
                if state["mode"] == "error":
                    status, body = 200, {"status": "timeout", "statusCode": 504}
                time.sleep(0.08 if overlap else 0.2)
                raw = json.dumps(body).encode()
                if state["mode"] == "oversize":
                    raw = b"x" * 65537
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            finally:
                with lock:
                    state["active"] -= 1

        def log_message(self, format, *args):
            pass

    class Server(ThreadingHTTPServer):
        request_queue_size = 64

    server = Server(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize(
    "mode", ["quota", "queue", "none", "malformed", "error", "oversize"]
)
def test_quota_gate_requires_a_real_quota_response(quota_server, tmp_path, mode):
    from nanolab.assets.diagnostics.native_quota_burst import run_burst

    url, state = quota_server
    state["mode"] = mode
    output = tmp_path / "receipt.json"
    if mode == "quota":
        run_burst(url, "owned-fn", {"input": {"text": "alpha beta"}}, output)
        receipt = json.loads(output.read_text())
        assert len(receipt["responses"]) == 30
        assert any(row["status"] == 200 for row in receipt["responses"])
        assert any(
            row["status"] == 429
            and json.loads(row["body"])["error"] == "invocation_quota_exceeded"
            for row in receipt["responses"]
        )
    else:
        with pytest.raises(RuntimeError):
            run_burst(url, "owned-fn", {"input": {"text": "alpha beta"}}, output)
        assert output.exists(), "failed qualifications must retain their observations"


def test_quota_task_uses_an_acquired_endpoint(quota_server, tmp_path):
    from sonata_engine import Resource, TaskInputs
    from sonata_tasks.execution.local import LocalCommandTaskExecutor

    from nanolab.tasks.platform import PlatformFunction
    from nanolab.tasks.recipes.workflow import RecipeDistribution
    from nanolab.tasks.validation.native_kubernetes import NativeKubernetesLifecycleTask

    url, _ = quota_server
    endpoint = Resource(
        title="Acquired endpoint",
        acquire=lambda _inputs: url,
        release=lambda *_args: None,
    )

    def unused_distribution(_inputs) -> RecipeDistribution:
        raise AssertionError("Quota subtask does not read the distribution")

    unused = Resource(
        title="Distribution", acquire=unused_distribution, release=lambda *_args: None
    )
    task = NativeKubernetesLifecycleTask(
        unused,
        namespace="owned",
        function=PlatformFunction("owned-fn", "fixed:tag", "{}", ("true",)),
        endpoint=endpoint,
        executor=LocalCommandTaskExecutor(),
        role="host",
        run_dir=tmp_path,
        function_component=("word-stats", "java"),
    )
    task._quota(TaskInputs._for_resources({endpoint: url}, {endpoint}), endpoint)
    assert (
        len(json.loads((tmp_path / "native/quota.json").read_text())["responses"]) == 30
    )
