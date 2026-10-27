"""The VM-side readiness probe, run for real against a fake platform."""

import base64
import hashlib
import json
import struct
import subprocess
import sys
import threading
import zlib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nanolab.workspace.paths import bundled_assets_root

SCRIPT = bundled_assets_root() / "diagnostics" / "experiment02_readiness.py"


def _png(size: int) -> bytes:
    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + b"IHDR"
        + header
        + struct.pack(">I", zlib.crc32(b"IHDR" + header))
    )


PNG = _png(128)
ENVELOPE = {
    "status": "success",
    "statusCode": 200,
    "encoding": "base64",
    "headers": {"content-type": "image/png"},
    "output": base64.b64encode(PNG).decode(),
}
# nanoFaaS answers HTTP 200 with an error envelope when the status code is null.
ERROR_ENVELOPE = {"status": "error", "statusCode": None, "error": {"code": "X"}}

Routes = dict[tuple[str, str], list[tuple[int, object]]]


class _Platform:
    """A fake platform answering each route from a queue; the last answer repeats."""

    def __init__(self, routes: Routes) -> None:
        self.routes = routes
        self.seen: list[tuple[str, str, dict[str, str]]] = []
        platform = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                _ = self.rfile.read(length)
                platform.seen.append((self.command, self.path, dict(self.headers)))
                queue = platform.routes.get((self.command, self.path), [(404, {})])
                status, value = queue.pop(0) if len(queue) > 1 else queue[0]
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = answer  # noqa: N815 - the handler API's names

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def platform() -> Iterator[_Platform]:
    fake = _Platform({})
    yield fake
    fake.server.shutdown()


def _run(tmp_path: Path, platform: _Platform, **overrides: object) -> dict:
    invoke = {
        "url": f"{platform.url}/invoke",
        "headers": {"Content-Type": "application/json"},
        "body": '{"text":"A","size":128}',
        "extractor": "nanofaas-envelope",
        "field": None,
        "idHeader": None,
        "size": 128,
        "pngSha256": None,
        "timeoutSeconds": 2,
    }
    invoke.update(overrides.pop("invoke", {}))  # type: ignore[arg-type]
    spec = {
        "start": [[sys.executable, "-c", "pass"]],
        "api": f"{platform.url}/health",
        "register": {
            "url": f"{platform.url}/v1/functions",
            "check": f"{platform.url}/v1/functions/qr-code-rust",
            "body": {"name": "qr-code-rust"},
        },
        "invoke": invoke,
        "timeoutSeconds": 5,
        "intervalSeconds": 0.05,
        "out": str(tmp_path / "first-response"),
        **overrides,
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    done = subprocess.run(
        (sys.executable, "-I", str(SCRIPT), str(path)),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def test_waits_for_a_correct_png_and_times_every_step_from_the_start(
    tmp_path, platform
):
    platform.routes.update(
        {
            ("GET", "/health"): [(503, {}), (200, {})],
            # The first registration outlived its client: the retry gets a 409.
            ("POST", "/v1/functions"): [(409, {"code": "FUNCTION_ALREADY_EXISTS"})],
            ("GET", "/v1/functions/qr-code-rust"): [(200, {"name": "qr-code-rust"})],
            ("POST", "/invoke"): [(200, ERROR_ENVELOPE), (200, ENVELOPE)],
        }
    )
    result = _run(tmp_path, platform)
    assert "timeout" not in result
    assert result["started"] < result["api"] <= result["registered"] <= result["first"]
    assert result["attempts"] == 2
    assert result["pngSha256"] == hashlib.sha256(PNG).hexdigest()
    assert json.loads((tmp_path / "first-response").read_text()) == ENVELOPE


def test_timeout_names_the_stage_and_the_last_answer(tmp_path, platform):
    platform.routes[("GET", "/health")] = [(503, {"status": "DOWN"})]
    result = _run(tmp_path, platform, timeoutSeconds=0.5)
    assert result["timeout"] == "api"
    assert result["last"]["status"] == 503
    assert "first" not in result
    assert not (tmp_path / "first-response").exists()


def test_a_failed_start_command_is_reported_before_any_probe(tmp_path, platform):
    result = _run(
        tmp_path, platform, start=[[sys.executable, "-c", "raise SystemExit(3)"]]
    )
    assert (result["failed"], result["code"]) == ("start", 3)
    assert platform.seen == []


def test_each_invocation_carries_its_own_execution_id(tmp_path, platform):
    platform.routes.update(
        {
            ("GET", "/health"): [(200, {})],
            ("POST", "/invoke"): [(429, {}), (200, ENVELOPE["output"])],
        }
    )
    result = _run(
        tmp_path,
        platform,
        register=None,
        invoke={"extractor": "json-base64", "idHeader": "X-Execution-Id"},
    )
    assert "first" in result
    ids = [
        headers["X-Execution-Id"]
        for method, path, headers in platform.seen
        if (method, path) == ("POST", "/invoke")
    ]
    assert ids == ["readiness-1", "readiness-2"]


def test_a_frozen_png_hash_must_match(tmp_path, platform):
    platform.routes.update(
        {("GET", "/health"): [(200, {})], ("POST", "/invoke"): [(200, ENVELOPE)]}
    )
    result = _run(
        tmp_path,
        platform,
        register=None,
        timeoutSeconds=0.5,
        invoke={"pngSha256": "0" * 64},
    )
    assert result["timeout"] == "first"


def test_serverledge_result_needs_its_inner_json_string(tmp_path, platform):
    platform.routes.update(
        {
            ("GET", "/health"): [(200, {})],
            ("POST", "/invoke"): [
                (200, {"Success": True, "Result": ENVELOPE["output"]}),
                (200, {"Success": True, "Result": json.dumps(ENVELOPE["output"])}),
            ],
        }
    )
    result = _run(
        tmp_path,
        platform,
        register=None,
        invoke={"extractor": "json-string-base64", "field": "Result"},
    )
    assert result["attempts"] == 2
    assert "first" in result
