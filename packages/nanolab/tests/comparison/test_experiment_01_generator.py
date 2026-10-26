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
