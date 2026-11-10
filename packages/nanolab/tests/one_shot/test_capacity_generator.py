"""Execute the workbench's k6 module with SDK and API responses, u64 bytes intact."""

import json
import subprocess
from pathlib import Path

import pytest

from nanolab.tasks.infra.ansible import workbench_collections_path

# Resolved at import, before the autouse fixture moves NANOLAB_WORKSPACE to a
# temporary directory: the collection the operator's workspace installed.
SCRIPT = (
    workbench_collections_path()
    / "ansible_collections/nanofaas/workbench/roles/loadgen/files/scripts/one-shot.js"
)


@pytest.fixture
def script() -> Path:
    """Return the collection's copy, installed by `nanolab.workbench.install`."""
    if not SCRIPT.is_file():
        pytest.skip(f"the nanofaas.workbench collection is not installed: {SCRIPT}")
    return SCRIPT


@pytest.mark.parametrize("sdk", [True, False])
def test_generator_correlates_sdk_capacity_and_gateway_requests(sdk, script):
    result = subprocess.run(
        [
            "node",
            "--experimental-vm-modules",
            str(Path(__file__).with_name("k6_harness.mjs")),
            str(script),
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
def test_every_returned_response_is_retained(body, status, script):
    result = subprocess.run(
        [
            "node",
            "--experimental-vm-modules",
            str(Path(__file__).with_name("k6_harness.mjs")),
            str(script),
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
