"""Synthetic provisioned helpers only: no Docker, network or JVM diagnostics."""

import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from nanolab.tasks.soak.diagnostics import DiagnosticRequest
from nanolab.tasks.soak.models import Target


def provision(tmp_path, body, *, runtime="jvm", operations=None, changes=None):
    from nanolab.tasks.soak.diagnostic_exec import (
        HelperProvisioning,
        ProvisionedDiagnosticExecutor,
    )

    helper = tmp_path / "helper.py"
    helper.write_text(
        "import base64,json,sys,time,os\n"
        "request=json.loads(sys.stdin.readline())\n"
        "target=request['target']\n"
        "if request['kind']=='inspect':\n"
        " print(json.dumps({'kind':'result','target':target,'running':True}))\n"
        "else:\n" + "\n".join(" " + line for line in body.splitlines()) + "\n"
    )
    capture = tmp_path / "captures"
    capture.mkdir()
    target = Target(
        "control-plane", "a" * 64, 123, "started", "image@sha256:" + "b" * 64, runtime
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
        "operations": operations or ["gc", "histogram"],
        "attach_verified": True,
        "runtime_compatible": True,
        "bounded_execution": True,
        "namespace_verified": True,
        "host_output_root": str(capture),
        "helper_output_root": str(capture),
        "output_mode": "framed-stdout",
        "private_control": runtime == "node",
        "control_transport": "private-pipe"
        if runtime == "node"
        else "target-namespace",
        "full_gc_source": "runtime-events",
        "file_output_quota_verified": False,
    }
    receipt.update(changes or {})
    path = tmp_path / "provisioning.json"
    path.write_text(json.dumps(receipt))
    pin = hashlib.sha256(path.read_bytes()).hexdigest()
    executor = ProvisionedDiagnosticExecutor(HelperProvisioning(path, pin, command))
    return executor, target, capture, helper


def request(target, capture, *, operation="histogram", max_bytes=4096, timeout_s=2):
    output = capture / "one"
    output.mkdir()
    return DiagnosticRequest(
        "request-1",
        target,
        operation,
        ("jcmd", "123", "GC.class_histogram"),
        None,
        output,
        max_bytes,
        timeout_s,
        time.monotonic(),
    )


SUCCESS = (
    "print(json.dumps({'kind':'result','target':target,'exit_code':0,"
    "'completed':True,'descendants_reaped':True}))"
)


def test_inspect_and_stream_artifacts(tmp_path):
    executor, target, capture, _ = provision(
        tmp_path,
        "print(json.dumps({'kind':'artifact','name':'histogram.txt','data_base64':base64.b64encode(b'histogram').decode()}))\n"
        + SUCCESS,
    )
    assert executor.inspect(target, 2) == target
    outcome = executor.execute(request(target, capture))
    assert outcome.completed
    assert (capture / "one" / "histogram.txt").read_bytes() == b"histogram"


def test_zero_exit_is_not_gc_completion(tmp_path):
    executor, target, capture, _ = provision(tmp_path, SUCCESS)
    outcome = executor.execute(request(target, capture, operation="gc"))
    assert outcome.completed is False
    assert outcome.full_gc_event is None


def test_gc_requires_matching_runtime_event(tmp_path):
    body = (
        "event={'schema':'nanolab-soak-v1','kind':'full_gc_completed','target':target,'request_id':request['request_id'],'source':'runtime-events','started_s':request['started_s'],'ended_s':time.monotonic()}\n"
        "print(json.dumps({'kind':'artifact','name':'full-gc.json','data_base64':base64.b64encode(json.dumps(event).encode()).decode()}))\n"
        "print(json.dumps({'kind':'result','target':target,'exit_code':0,'completed':True,'descendants_reaped':True,'before_count':2,'after_count':3,'full_gc_event':'full-gc.json'}))"
    )
    executor, target, capture, _ = provision(tmp_path, body)
    outcome = executor.execute(request(target, capture, operation="gc"))
    assert outcome.completed and outcome.full_gc_event == "full-gc.json"


def test_gc_event_for_another_request_is_rejected(tmp_path):
    body = (
        "event={'schema':'nanolab-soak-v1','kind':'full_gc_completed','target':target,'request_id':'wrong','source':'runtime-events','started_s':request['started_s'],'ended_s':time.monotonic()}\n"
        "print(json.dumps({'kind':'artifact','name':'full-gc.json','data_base64':base64.b64encode(json.dumps(event).encode()).decode()}))\n"
        "print(json.dumps({'kind':'result','target':target,'exit_code':0,'completed':True,'descendants_reaped':True,'before_count':2,'after_count':3,'full_gc_event':'full-gc.json'}))"
    )
    executor, target, capture, _ = provision(tmp_path, body)
    outcome = executor.execute(request(target, capture, operation="gc"))
    assert not outcome.completed and outcome.full_gc_event is None


def test_output_budget_is_enforced_before_artifact_write(tmp_path):
    body = (
        "print(json.dumps({'kind':'artifact','name':'large.bin','data_base64':base64.b64encode(b'x'*100).decode()}))\n"
        + SUCCESS
    )
    executor, target, capture, _ = provision(tmp_path, body)
    with pytest.raises(OSError, match=r"diagnostic artifact byte budget"):
        executor.execute(request(target, capture, max_bytes=8))
    assert not (capture / "one" / "large.bin").exists()


def test_traversal_is_rejected(tmp_path):
    body = (
        "print(json.dumps({'kind':'artifact','name':'../escaped','data_base64':'eA=='}))\n"
        + SUCCESS
    )
    executor, target, capture, _ = provision(tmp_path, body)
    with pytest.raises(ValueError, match="unsafe diagnostic artifact name"):
        executor.execute(request(target, capture))
    assert not (capture / "escaped").exists()


def test_helper_timeout_is_bounded(tmp_path):
    executor, target, capture, _ = provision(tmp_path, "time.sleep(30)")
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="diagnostic helper deadline exceeded"):
        executor.execute(request(target, capture, timeout_s=0.2))
    assert time.monotonic() - started < 2


def test_changed_executable_is_rejected_before_execution(tmp_path):
    executor, target, _capture, helper = provision(tmp_path, SUCCESS)
    helper.write_text("raise RuntimeError('must never execute')")
    with pytest.raises(ValueError, match="helper executable identity changed"):
        executor.inspect(target, 2)


@pytest.mark.parametrize(
    "changes",
    [
        {"private_control": False},
        {"control_transport": "public-tcp"},
        {"namespace_verified": False},
    ],
)
def test_unprovisioned_node_control_is_unavailable(tmp_path, changes):
    with pytest.raises(ValueError, match=r"private control|namespace_verified"):
        provision(tmp_path, SUCCESS, runtime="node", changes=changes)


def test_file_output_diagnostics_require_verified_target_quota(tmp_path):
    executor, target, capture, _ = provision(
        tmp_path, SUCCESS, operations=["heap_dump"]
    )
    with pytest.raises(ValueError, match=r"file diagnostics require verified"):
        executor.execute(request(target, capture, operation="heap_dump"))


def test_wrong_process_identity_is_rejected(tmp_path):
    executor, target, capture, _ = provision(
        tmp_path, "target['process_id']=456\n" + SUCCESS
    )
    with pytest.raises(ValueError, match=r"diagnostic completion target identity"):
        executor.execute(request(target, capture))


def test_capabilities_exclude_file_operations_without_quota(tmp_path):
    executor, _target, _capture, _ = provision(
        tmp_path, SUCCESS, operations=["gc", "heap_dump", "jfr"]
    )
    assert executor.capabilities().operations == frozenset({"gc"})


def test_node_cannot_request_heap_snapshot_as_gc(tmp_path):
    from dataclasses import replace

    executor, target, capture, _ = provision(
        tmp_path, SUCCESS, runtime="node", operations=["gc"]
    )
    attempt = replace(
        request(target, capture, operation="gc"),
        protocol_method="HeapProfiler.takeHeapSnapshot",
    )
    with pytest.raises(ValueError, match=r"unprovisioned private inspector"):
        executor.execute(attempt)


@pytest.mark.parametrize("exit_mode", ["timeout", "interrupt", "normal"])
def test_detached_descendants_are_reaped_and_unrelated_process_is_preserved(
    tmp_path, monkeypatch, exit_mode
):
    import os
    import selectors
    import signal
    import subprocess

    pid_path = tmp_path / "detached.pid"
    body = (
        "import subprocess\n"
        f"child=subprocess.Popen([{sys.executable!r},'-c',"
        "'import time; time.sleep(30)'],start_new_session=True,"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_path)!r},'w').write(str(child.pid))\n"
        + (SUCCESS if exit_mode == "normal" else "time.sleep(30)")
    )
    executor, target, capture, _ = provision(tmp_path, body)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    original_select = selectors.DefaultSelector.select
    if exit_mode == "interrupt":

        def interrupt_when_detached(selector, timeout=None):
            if pid_path.exists() and pid_path.stat().st_size:
                raise KeyboardInterrupt("synthetic cancellation")
            return original_select(selector, timeout)

        monkeypatch.setattr(
            selectors.DefaultSelector, "select", interrupt_when_detached
        )
    try:
        expected = (
            TimeoutError
            if exit_mode == "timeout"
            else KeyboardInterrupt
            if exit_mode == "interrupt"
            else OSError
        )
        with pytest.raises(
            expected,
            match=(
                r"diagnostic helper deadline exceeded|"
                r"diagnostic helper required forced|"
                r"synthetic cancellation"
            ),
        ):
            executor.execute(
                request(target, capture, timeout_s=0.5 if exit_mode == "timeout" else 3)
            )
        pid = int(pid_path.read_text())
        assert not Path(f"/proc/{pid}").exists(), (
            "detached diagnostic descendant survived or was not reaped"
        )
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=2)
        # Contain the deliberately failing RED run too; never signal global names.
        if pid_path.exists() and pid_path.stat().st_size:
            pid = int(pid_path.read_text())
            try:
                descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                pass
            else:
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    os.close(descriptor)
