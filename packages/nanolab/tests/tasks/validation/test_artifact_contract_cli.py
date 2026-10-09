from __future__ import annotations

import json
import subprocess
import sys

from tests.tasks.validation.test_artifact_contract_capture import capture


def test_probe_accepts_owned_runner_instruction_argument():
    from nanolab.assets.diagnostics import artifact_contract

    with capture() as base:
        instruction = {
            "url": base + "/health",
            "settings": {"request_seconds": 1, "message_bytes": 2048},
        }
        result = subprocess.run(
            (
                sys.executable,
                artifact_contract.__file__,
                "probe",
                json.dumps(instruction),
            ),
            input="",
            capture_output=True,
            text=True,
            timeout=2,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == 200


def test_probe_failure_is_structured_without_traceback():
    from nanolab.assets.diagnostics import artifact_contract

    instruction = {
        "url": "unsupported://wrong",
        "settings": {"request_seconds": 1, "message_bytes": 2048},
    }
    result = subprocess.run(
        (sys.executable, artifact_contract.__file__, "probe", json.dumps(instruction)),
        input="",
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert json.loads(result.stdout)["error"]
