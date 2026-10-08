from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest
from sonata_engine import Resource, TaskInputs

from nanolab.config.contract import ContractConfig
from nanolab.images.plan import build_image_plan
from nanolab.workspace.recipe import RecipeRun
from tests.functions.test_contracts import frozen_fixture as frozen_fixture
from tests.tasks.validation.test_artifact_contract_capture import capture, request
from tests.tasks.validation.test_contract_resources import ARCH, DockerBoundary


@contextmanager
def artifact_server(capture_base, *, wrong=False):
    class Artifact(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ready":true}')

        def do_POST(self):
            assert self.path == "/invoke"
            assert json.loads(self.rfile.read(int(self.headers["Content-Length"]))) == {
                "input": {}
            }
            payload = {"answer": 2 if wrong else 1}
            execution_id = self.headers["X-Execution-Id"]
            assert (
                request(
                    capture_base,
                    f"/v1/executions/{execution_id}:complete",
                    {"success": True, "output": payload},
                )[0]
                == 200
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Artifact)
    server.daemon_threads = True
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


class HttpBoundary(DockerBoundary):
    def __init__(self, capture_base, artifact_base, *, duplicate=False, pending=False):
        super().__init__()
        self.capture_base, self.artifact_base = capture_base, artifact_base
        self.duplicate = duplicate
        self.execution_id = None
        self.pending = pending
        self.pending_socket = None

    def dispatch(self, argv, config):
        if argv[:2] == ("docker", "exec"):
            from nanolab.assets.diagnostics.artifact_contract import probe

            instruction = json.loads(argv[-1])
            instruction["url"] = (
                instruction["url"]
                .replace("http://capture:8081", self.capture_base)
                .replace("http://artifact:8080", self.artifact_base)
            )
            if instruction["url"].endswith("/invoke"):
                self.execution_id = instruction["headers"]["X-Execution-Id"]
            return json.dumps(probe(instruction, instruction["settings"])), 0
        if argv[:2] == ("docker", "stop") and self.duplicate:
            assert (
                request(
                    self.capture_base,
                    f"/v1/executions/{self.execution_id}:complete",
                    {"success": True, "output": {"answer": 1}},
                )[0]
                == 200
            )
        if argv[:2] == ("docker", "stop") and self.pending:
            self.pending_socket = socket.create_connection(
                ("127.0.0.1", int(self.capture_base.rsplit(":", 1)[1]))
            )
            self.pending_socket.sendall(
                (
                    f"POST /v1/executions/{self.execution_id}:complete HTTP/1.1\r\n"
                    "Host: capture\r\nContent-Length: 200\r\n\r\n{"
                ).encode()
            )
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                if json.loads(request(self.capture_base, "/_nanolab/records")[1])[
                    "activeCallbacks"
                ]:
                    break
        result = super().dispatch(argv, config)
        if argv[:2] == ("docker", "create"):
            self.objects[result[0]]["Config"]["Entrypoint"] = ["/app/function"]
        return result


def execute(source_root, run_dir, boundary):
    from nanolab.tasks.validation.contract_resources import (
        ContractImage,
        ContractRuntime,
    )
    from nanolab.tasks.validation.function_contracts import FunctionContractsTask

    source_value = RecipeRun(
        source_root, run_dir / "scenario.yaml", run_dir / "dist", "test"
    )
    cell = build_image_plan(
        source_root,
        "1.0.0",
        registry="nanolab-contract/test",
        selectors=("python-word-stats",),
        architectures=(ARCH,),
    ).cells[0]
    image = ContractImage(cell, "image-id", ARCH, ("/app/function",), None)
    boundary.objects[cell.image] = {
        "Id": "image-id",
        "Architecture": ARCH,
        "Config": {
            "Entrypoint": ["/app/function"],
            "Labels": {"nanolab.contract.owner": "test"},
        },
    }
    values = [
        source_value,
        (image,),
        ContractRuntime("network", "capture", "helper", "test"),
    ]
    resources = tuple(
        Resource(
            title=f"resource-{index}",
            acquire=lambda _, value=value: value,
            release=lambda *_: None,
        )
        for index, value in enumerate(values)
    )
    inputs = TaskInputs._for_resources(
        dict(zip(resources, values, strict=True)), set(resources)
    )
    task = FunctionContractsTask(
        *resources,
        selectors=("word-stats",),
        settings=ContractConfig(
            readiness_seconds=1,
            request_seconds=1,
            callback_seconds=1,
            quiet_seconds=0.01,
        ),
        executor=boundary,
        run_dir=run_dir,
    )
    return task.run(inputs).value


@pytest.mark.parametrize("fault", ["none", "body", "late-duplicate", "pending"])
def test_runtime_exchange_and_final_audit(frozen_fixture, tmp_path, fault):
    run_dir = tmp_path / "run"
    with (
        capture(request_seconds=1) as callback_base,
        artifact_server(callback_base, wrong=fault == "body") as artifact_base,
    ):
        boundary = HttpBoundary(
            callback_base,
            artifact_base,
            duplicate=fault == "late-duplicate",
            pending=fault == "pending",
        )
        if fault == "none":
            result = execute(frozen_fixture, run_dir, boundary)
            assert result is not None
            assert result.expected_http == 1
            assert result.expected_callbacks == 1
            assert (
                json.loads((run_dir / "case-index.json").read_text())["status"]
                == "passed"
            )
        else:
            try:
                with pytest.raises(ValueError, match=r"body|callback"):
                    execute(frozen_fixture, run_dir, boundary)
            finally:
                if boundary.pending_socket is not None:
                    boundary.pending_socket.close()
            assert (
                json.loads((run_dir / "case-index.json").read_text())["status"]
                == "failed"
            )
        assert not (run_dir / "qualification.json").exists()
        assert list((run_dir / "cases").rglob("receipt.json"))
        assert list((run_dir / "capture").glob("*.raw"))


def test_one_shot_uses_real_entrypoint_and_wrapped_input(tmp_path):
    from nanolab.images.plan import ImageCell, ImageTarget
    from nanolab.tasks.validation.contract_resources import (
        ContractImage,
        ContractRuntime,
        artifact_container_resource,
    )

    boundary = DockerBoundary()
    cell = ImageCell(
        ImageTarget("bash-word-stats", ("default",), tmp_path / "Dockerfile", tmp_path),
        ARCH,
        "default",
        "tag",
        "image",
    )
    image = ContractImage(cell, "image-id", ARCH, ("/app/watchdog",), None)
    resource = artifact_container_resource(
        image,
        ContractRuntime("network", "capture", "helper", "test"),
        mode="one-shot",
        execution_id="case-id",
        payload={"input": {"text": "hello"}},
        executor=boundary,
        run_dir=tmp_path,
    )
    inputs = TaskInputs.empty()
    value = resource.acquire(inputs)
    create = next(
        command["argv"]
        for command in boundary.commands
        if command["argv"][:2] == ["docker", "create"]
    )
    assert "WARM=false" in create
    assert 'INVOCATION_PAYLOAD={"input": {"text": "hello"}}' in create
    assert create[-1] == "image-id"
    assert "--entrypoint" not in create
    resource.release(inputs, value)
