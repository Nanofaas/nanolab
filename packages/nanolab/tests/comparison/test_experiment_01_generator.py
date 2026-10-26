"""The shipped generator records completed HTTP responses even when malformed."""

import json
import shutil
from pathlib import Path

import pytest
from sonata_tasks.shell import SubprocessShell

from nanolab.workspace.paths import bundled_assets_root

PNG_ENVELOPE = {
    "status": "success",
    "statusCode": 200,
    "encoding": "base64",
    "headers": {"content-type": "image/png"},
    "output": "iVBORw0KGgoAAAAASUhEUgAABAAAAAQA",
}
PHASES = [
    {"name": "idle", "rate": 0, "durationSeconds": 60, "startSeconds": 0},
    {"name": "warmup", "rate": 20, "durationSeconds": 60, "startSeconds": 60},
    {"name": "step-01", "rate": 25, "durationSeconds": 60, "startSeconds": 120},
    {"name": "drain", "rate": 0, "durationSeconds": 60, "startSeconds": 180},
]
ENVIRONMENT = {
    "E01_PROTOCOL": json.dumps(
        {
            "phases": PHASES,
            "httpTimeoutSeconds": 30,
            "gracefulStopSeconds": 40,
            "preAllocatedVUs": 100,
            "maxVUs": 6000,
            "size": 1024,
        }
    ),
    "E01_PAYLOAD": '{"size":1024,"text":"A"}',
    "NANOFAAS_URL": "http://10.0.0.4:30080",
    "NANOFAAS_FUNCTION": "qr-code-rust",
}


def _run(sample: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Requires Node to execute the shipped k6 script")
    result = SubprocessShell().run(
        [
            node,
            "--experimental-vm-modules",
            str(Path(__file__).with_name("experiment_01_k6_harness.mjs")),
            str(bundled_assets_root() / "k6" / "experiment-01.js"),
            json.dumps(sample),
        ]
    )
    assert result.return_code == 0, result.stderr
    observed = json.loads(result.stdout)
    assert "error" not in observed
    return observed


@pytest.mark.parametrize(
    ("sample", "correct"),
    [
        ({"body": None}, False),
        ({"body": []}, False),
        ({"body": "unexpected"}, False),
        ({"invalidJson": True}, False),
        ({"body": {**PNG_ENVELOPE, "output": "iVBORw0KGgo!"}}, False),
        ({"body": PNG_ENVELOPE}, True),
        ({"body": PNG_ENVELOPE, "status": 503}, False),
    ],
)
def test_http_response_always_emits_one_completed_terminal(sample, correct):
    observed = _run(sample)
    assert observed["events"] == [
        {"t": "send", "id": "warmup-0", "phase": "warmup", "ts": 100000},
        {
            "t": "done",
            "id": "warmup-0",
            "phase": "warmup",
            "ts": 100000,
            "dur": 100,
            "status": sample.get("status", 200),
            "ok": correct,
        },
    ]


def _scenario(name: str, rate: int, start: int) -> dict:
    return {
        "executor": "constant-arrival-rate",
        "rate": rate,
        "timeUnit": "1s",
        "duration": "60s",
        "startTime": f"{start}s",
        "preAllocatedVUs": 100,
        "maxVUs": 6000,
        "gracefulStop": "40s",
        "exec": "invoke",
        "tags": {"phase": name},
    }


def test_shared_generator_preserves_experiment_01_defaults():
    observed = _run({"env": ENVIRONMENT, "body": PNG_ENVELOPE})
    assert observed["request"] == {
        "url": "http://10.0.0.4:30080/v1/functions/qr-code-rust:invoke",
        "payload": '{"size":1024,"text":"A"}',
        "params": {
            "headers": {"Content-Type": "application/json"},
            "timeout": "30s",
            "tags": {"phase": "warmup"},
            # k6's default, named: the nanoFaaS envelope is text.
            "responseType": "text",
        },
    }
    assert observed["options"] == {
        "scenarios": {
            "warmup": _scenario("warmup", 20, 60),
            "step-01": _scenario("step-01", 25, 120),
            "drain": {
                "executor": "constant-vus",
                "vus": 1,
                "duration": "60s",
                "startTime": "180s",
                "gracefulStop": "0s",
                "exec": "wait",
                "tags": {"phase": "drain"},
            },
        },
        "thresholds": {
            "dropped_iterations{scenario:warmup}": ["count>=0"],
            "http_reqs{scenario:warmup}": ["count>=0"],
            "dropped_iterations{scenario:step-01}": ["count>=0"],
            "http_reqs{scenario:step-01}": ["count>=0"],
        },
        "discardResponseBodies": False,
    }
    assert observed["events"][1]["ok"] is True


def test_target_selection_does_not_change_calendar_or_send_cohorts():
    target = {
        "url": "http://10.0.0.4:30080/invoke",
        "headers": {"Content-Type": "application/json"},
        "extractor": "json-base64",
        "field": None,
        "idHeader": None,
        "pngSha256": None,
    }
    default = _run({"env": ENVIRONMENT, "body": PNG_ENVELOPE})
    targeted = _run(
        {
            "env": {**ENVIRONMENT, "EXPERIMENT_TARGET": json.dumps(target)},
            "body": PNG_ENVELOPE["output"],
        }
    )
    assert targeted["options"] == default["options"]
    assert targeted["events"][0] == default["events"][0]
    assert targeted["request"]["url"] == target["url"]
    assert targeted["events"][1]["ok"] is True
