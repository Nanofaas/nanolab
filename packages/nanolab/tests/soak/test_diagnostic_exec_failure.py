"""Failure instrumentation with synthetic local helpers only, never Docker."""

import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from nanolab.tasks.soak.diagnostic_exec import (
    HelperProvisioning,
    ProvisionedDiagnosticExecutor,
)
from nanolab.tasks.soak.diagnostics import DiagnosticRequest
from nanolab.tasks.soak.models import Target


def provision(tmp_path, body):
    """Create a pinned synthetic helper without importing collected tests."""
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import base64,json,sys\n"
        "request=json.loads(sys.stdin.readline())\n"
        "target=request['target']\n"
        "if request['kind']=='inspect':\n"
        " print(json.dumps({'kind':'result','target':target,'running':True}))\n"
        "else:\n" + "\n".join(" " + line for line in body.splitlines()) + "\n"
    )
    capture = tmp_path / "captures"
    capture.mkdir()
    target = Target(
        "control-plane", "a" * 64, 123, "started", "image@sha256:" + "b" * 64, "jvm"
    )
    command = (sys.executable, str(helper))
    receipt = {
        "schema": "nanolab-soak-diagnostic-helper-v1",
        "target": asdict(target),
        "command": list(command),
        "helper_digest": "helper@sha256:" + "c" * 64,
        "command_artifacts": [
            {
                "path": path,
                "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            }
            for path in command
        ],
        "operations": ["histogram"],
        "attach_verified": True,
        "runtime_compatible": True,
        "bounded_execution": True,
        "namespace_verified": True,
        "host_output_root": str(capture),
        "helper_output_root": str(capture),
        "output_mode": "framed-stdout",
        "private_control": False,
        "control_transport": "target-namespace",
        "file_output_quota_verified": False,
    }
    path = tmp_path / "provisioning.json"
    path.write_text(json.dumps(receipt))
    pin = hashlib.sha256(path.read_bytes()).hexdigest()
    executor = ProvisionedDiagnosticExecutor(HelperProvisioning(path, pin, command))
    return executor, target, capture, helper


def request(target, capture, *, max_bytes=4096):
    output = capture / "one"
    output.mkdir()
    return DiagnosticRequest(
        "request-1",
        target,
        "histogram",
        ("jcmd", "123", "GC.class_histogram"),
        None,
        output,
        max_bytes,
        2,
        time.monotonic(),
    )


def failure_body(error="No space left on device", prefix="", **fields):
    result = {
        "kind": "result",
        "exit_code": 1,
        "completed": False,
        "remote_finished": False,
        "descendants_reaped": False,
        "error": error,
        **fields,
    }
    return (
        prefix
        + f"result={result!r}\nresult['target']=target\nprint(json.dumps(result))"
    )


def test_remote_failure_is_preserved_and_still_raises(tmp_path):
    executor, target, capture, _ = provision(tmp_path, failure_body())
    attempt = request(target, capture)
    with pytest.raises(RuntimeError, match="remote diagnostic failed"):
        executor.execute(attempt)
    saved = json.loads((attempt.output_dir / "remote-failure.json").read_bytes())
    assert saved["target"] == asdict(target)
    assert saved["request_id"] == attempt.request_id
    assert saved["operation"] == attempt.operation
    assert saved["completed"] is False
    assert saved["remote_finished"] is False
    assert saved["descendants_reaped"] is False
    assert saved["_local_descendants_reaped"] is True


def test_failure_summary_bounds_error_and_omits_payload(tmp_path):
    executor, target, capture, _ = provision(
        tmp_path, failure_body("x" * 20000, data_base64="y" * 20000)
    )
    attempt = request(target, capture, max_bytes=16384)
    with pytest.raises(RuntimeError, match="remote diagnostic failed"):
        executor.execute(attempt)
    body = (attempt.output_dir / "remote-failure.json").read_bytes()
    saved = json.loads(body)
    assert len(body) <= 16384
    assert len(saved["error"]) == 2048
    assert saved["error_truncated"] is True
    assert "data_base64" not in saved


@pytest.mark.parametrize(
    ("prefix", "fields"),
    [("target['process_id']=456\n", {}), ("", {"request_id": "wrong"})],
)
def test_failure_identity_mismatch_is_not_saved(tmp_path, prefix, fields):
    executor, target, capture, _ = provision(
        tmp_path, failure_body(prefix=prefix, **fields)
    )
    attempt = request(target, capture)
    with pytest.raises(ValueError, match=r"diagnostic failure result identity"):
        executor.execute(attempt)
    assert not (attempt.output_dir / "remote-failure.json").exists()


def test_failure_evidence_does_not_exceed_request_budget(tmp_path):
    executor, target, capture, _ = provision(tmp_path, failure_body())
    attempt = request(target, capture, max_bytes=8)
    with pytest.raises(RuntimeError, match="remote diagnostic failed") as caught:
        executor.execute(attempt)
    assert not (attempt.output_dir / "remote-failure.json").exists()
    assert any("budget" in note for note in caught.value.__notes__)


def test_failure_evidence_never_overwrites_remote_artifact(tmp_path):
    prefix = (
        "print(json.dumps({'kind':'artifact','name':'remote-failure.json',"
        "'data_base64':base64.b64encode(b'original').decode()}))\n"
    )
    executor, target, capture, _ = provision(tmp_path, failure_body(prefix=prefix))
    attempt = request(target, capture)
    with pytest.raises(RuntimeError, match="remote diagnostic failed") as caught:
        executor.execute(attempt)
    assert (attempt.output_dir / "remote-failure.json").read_bytes() == b"original"
    assert any("not saved" in note for note in caught.value.__notes__)
