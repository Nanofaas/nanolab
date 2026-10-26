"""The shipped generator judges each platform's response by the PNG it carries."""

import base64
import hashlib
import json
import shutil
import struct
import zlib
from pathlib import Path

import pytest
from sonata_tasks.shell import SubprocessShell

from nanolab.workspace.paths import bundled_assets_root

# Experiment 02 runs experiment 01's script, with a target.
SCRIPT = bundled_assets_root() / "k6" / "experiment-01.js"
HARNESS = Path(__file__).parents[2] / "comparison" / "experiment_01_k6_harness.mjs"


def _png(size: int) -> bytes:
    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    chunk = (
        struct.pack(">I", len(header))
        + b"IHDR"
        + header
        + struct.pack(">I", zlib.crc32(b"IHDR" + header))
    )
    return b"\x89PNG\r\n\x1a\n" + chunk


PNG = _png(128)
B64 = base64.b64encode(PNG).decode()
SHA = hashlib.sha256(PNG).hexdigest()
URL = "http://10.0.0.4:8080/v1/functions/qr-code-rust:invoke"
HEADERS = {"Content-Type": "application/json"}
ENVELOPE = {
    "status": "success",
    "statusCode": 200,
    "encoding": "base64",
    "headers": {"content-type": "image/png"},
    "output": B64,
}


def _target(
    extractor: str,
    field: str | None = None,
    sha: str | None = None,
    id_header: str | None = None,
):
    return {
        "url": URL,
        "headers": HEADERS,
        "extractor": extractor,
        "field": field,
        "idHeader": id_header,
        "pngSha256": sha,
    }


def _run(sample: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Requires Node to execute the shipped k6 script")
    environment = {
        "E01_PROTOCOL": json.dumps(
            {"phases": [], "httpTimeoutSeconds": 30, "size": 128}
        ),
        "E01_PAYLOAD": '{"text":"A","size":128}',
        "EXPERIMENT_TARGET": json.dumps(sample["target"]),
    }
    harness = {key: value for key, value in sample.items() if key != "target"}
    result = SubprocessShell().run(
        [
            node,
            "--experimental-vm-modules",
            str(HARNESS),
            str(SCRIPT),
            json.dumps({"env": environment, **harness}),
        ]
    )
    assert result.return_code == 0, result.stderr
    observed = json.loads(result.stdout)
    assert "error" not in observed
    return observed


@pytest.mark.parametrize(
    ("sample", "correct"),
    [
        ({"target": _target("nanofaas-envelope"), "body": ENVELOPE}, True),
        (
            {
                "target": _target("nanofaas-envelope"),
                "body": {**ENVELOPE, "output": "iVBORw0KGgo!"},
            },
            False,
        ),
        (
            {"target": _target("nanofaas-envelope"), "body": ENVELOPE, "status": 503},
            False,
        ),
        ({"target": _target("json-base64"), "body": B64}, True),
        ({"target": _target("json-base64", "output"), "body": {"output": B64}}, True),
        (
            {
                "target": _target("json-base64", "output"),
                "body": {"output": B64, "error": {"code": "HANDLER_ERROR"}},
            },
            False,
        ),
        (
            {
                "target": _target("raw-png"),
                "raw": B64,
                "headers": {"Content-Type": "image/png"},
            },
            True,
        ),
        (
            {
                "target": _target("raw-png"),
                "raw": B64,
                "headers": {"Content-Type": "text/plain"},
            },
            False,
        ),
        (
            {
                "target": _target("raw-png"),
                "raw": base64.b64encode(_png(64)).decode(),
                "headers": {"Content-Type": "image/png"},
            },
            False,
        ),
        ({"target": _target("json-base64", sha=SHA), "body": B64}, True),
        ({"target": _target("json-base64", sha="0" * 64), "body": B64}, False),
    ],
)
def test_each_extractor_judges_the_png(sample, correct):
    events = _run(sample)["events"]
    assert events[0] == {"t": "send", "id": "warmup-0", "phase": "warmup", "ts": 100000}
    assert events[1]["t"] == "done"
    assert events[1]["ok"] is correct


def test_the_request_goes_to_the_target_with_its_headers():
    raw = _run(
        {
            "target": _target("raw-png"),
            "raw": B64,
            "headers": {"Content-Type": "image/png"},
        }
    )["request"]
    assert raw == {
        "url": URL,
        "payload": '{"text":"A","size":128}',
        "params": {
            "headers": HEADERS,
            "timeout": "30s",
            "tags": {"phase": "warmup"},
            "responseType": "binary",
        },
    }
    text = _run({"target": _target("json-base64"), "body": B64})["request"]
    assert text["params"]["responseType"] == "text"


def test_each_request_carries_its_own_execution_id():
    # The function's runtime admits one active execution per id: a shared id
    # turns every overlap into a 429.
    observed = _run(
        {"target": _target("json-base64", id_header="X-Execution-Id"), "body": B64}
    )
    assert observed["request"]["params"]["headers"] == {
        **HEADERS,
        "X-Execution-Id": "warmup-0",
    }
    assert observed["events"][0]["id"] == "warmup-0"
