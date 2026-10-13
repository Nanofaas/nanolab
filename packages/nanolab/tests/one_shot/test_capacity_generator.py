"""Execute the bundled k6 module with SDK and API responses, preserving u64 bytes."""

import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("sdk", [True, False])
def test_generator_correlates_sdk_capacity_and_gateway_requests(sdk):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            "--experimental-vm-modules",
            str(Path(__file__).with_name("k6_harness.mjs")),
            str(root / "src/nanolab/assets/k6/one-shot.js"),
            json.dumps(
                {
                    "sdk": sdk,
                    "url": "http://runtime/invoke",
                    "originalId": "original-1",
                    "scheduledAt": 99.9,
                    "input": {},
                }
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(result.stdout)
    assert output["result"]["outputJson"] == "10654779122033555500"
    assert output["result"]["responseStatus"] == "success"
    assert output["result"]["executionId"] == ("original-1" if sdk else "api-id")
    assert output["result"]["terminalExecutionId"] == (
        "original-1" if sdk else "api-id"
    )
    assert output["headers"]["X-Trace-Id"] == "original-1"
    if sdk:
        assert output["headers"]["X-Execution-Id"] == "original-1"
        assert output["headers"]["X-Dispatch-Attempt"] == "1"


@pytest.mark.parametrize("body", [None, [], 7, "text", {"status": "error"}])
@pytest.mark.parametrize("status", [200, 429, 500, 0])
def test_every_returned_response_is_retained(body, status):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            "--experimental-vm-modules",
            str(Path(__file__).with_name("k6_harness.mjs")),
            str(root / "src/nanolab/assets/k6/one-shot.js"),
            json.dumps(
                {
                    "testResponse": body,
                    "testStatus": status,
                    "url": "http://runtime/invoke",
                    "originalId": "original-1",
                    "scheduledAt": 99.9,
                    "input": {},
                }
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    observed = json.loads(result.stdout)["result"]
    assert observed["event"] == "result"
    assert observed["statusCode"] == status
    assert json.loads(observed["responseBody"]) == body
