"""Real subprocess exercise of the in-container MAT worker script.

No Docker and no real MAT/HPROF: ParseHeapDump.sh is a fake stdlib script
that records its argv and drops a small ZIP, standing in for the real
launcher's report output. NANOLAB_MAT_WORK_DIR overrides the worker's fixed
container path (/work) so the test can point it at an owned tmp_path.
"""

import hashlib
import json
import os
import subprocess
import sys

from nanolab.workspace.paths import bundled_assets_root

WORKER = bundled_assets_root() / "soak" / "mat-worker.py"

FAKE_LAUNCHER = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path


recorder = Path(os.environ["MAT_TEST_RECORDER"])
args = sys.argv[1:]
with recorder.open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
if os.environ.get("MAT_TEST_MODE") == "fail" and args[-1].endswith("suspects"):
    sys.exit(3)
name = args[0].split(".")[0] + "-" + args[-1].split(":")[-1] + ".zip"
Path(name).write_bytes(b"zip-bytes-for-" + args[-1].encode())
"""

EXPECTED_ARGS = [
    ("baseline.hprof", "org.eclipse.mat.api:overview"),
    ("baseline.hprof", "org.eclipse.mat.api:suspects"),
    ("baseline.hprof", "org.eclipse.mat.api:top_components"),
    ("final.hprof", "org.eclipse.mat.api:overview"),
    ("final.hprof", "org.eclipse.mat.api:suspects"),
    ("final.hprof", "org.eclipse.mat.api:top_components"),
    (
        "final.hprof",
        "-snapshot2=baseline.hprof",
        "org.eclipse.mat.api:compare",
    ),
    (
        "final.hprof",
        "-baseline=baseline.hprof",
        "org.eclipse.mat.api:suspects2",
    ),
]


def _run(tmp_path, *, mode="ok"):
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    baseline = tmp_path / "baseline.hprof"
    baseline.write_bytes(b"baseline-bytes")
    final = tmp_path / "final.hprof"
    final.write_bytes(b"final-bytes")
    launcher = tmp_path / "ParseHeapDump.sh"
    launcher.write_text(FAKE_LAUNCHER)
    launcher.chmod(0o755)
    recorder = tmp_path / "recorder.jsonl"
    env = dict(os.environ)
    env["NANOLAB_MAT_WORK_DIR"] = str(work)
    env["MAT_TEST_RECORDER"] = str(recorder)
    env["MAT_TEST_MODE"] = mode
    result = subprocess.run(
        [
            sys.executable,
            str(WORKER),
            str(baseline),
            str(final),
            str(out),
            str(launcher),
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, work, out, recorder


def test_worker_runs_the_eight_fixed_mat_report_invocations(tmp_path):
    result, _work, _out, recorder = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    recorded = [tuple(json.loads(line)) for line in recorder.read_text().splitlines()]
    assert recorded == EXPECTED_ARGS


def test_worker_copies_only_zip_reports_and_removes_work_copies(tmp_path):
    result, work, out, _recorder = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    reports = sorted(path.name for path in (out / "reports").iterdir())
    assert len(reports) == 8
    assert all(name.endswith(".zip") for name in reports)
    assert not (work / "baseline.hprof").exists()
    assert not (work / "final.hprof").exists()
    assert list(work.iterdir()) == []


def test_worker_writes_a_receipt_binding_hashes_to_each_invocation(tmp_path):
    result, _work, out, _recorder = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((out / "mat-worker-receipt.json").read_text())
    assert len(receipt["invocations"]) == 8
    for invocation in receipt["invocations"]:
        assert invocation["returncode"] == 0
        assert invocation["outputs"]
        for output in invocation["outputs"]:
            path = out / "reports" / output["name"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert output["sha256"] == digest
            assert output["size_bytes"] == path.stat().st_size


def test_worker_keeps_partial_evidence_and_cleans_up_on_a_failing_report(tmp_path):
    result, work, out, _recorder = _run(tmp_path, mode="fail")
    assert result.returncode != 0
    receipt = json.loads((out / "mat-worker-receipt.json").read_text())
    # baseline overview succeeded before baseline suspects failed and aborted.
    assert len(receipt["invocations"]) == 2
    assert receipt["invocations"][0]["returncode"] == 0
    assert receipt["invocations"][0]["outputs"]
    assert receipt["invocations"][1]["returncode"] == 3
    assert receipt["invocations"][1]["outputs"] == []
    assert len(list((out / "reports").iterdir())) == 1
    assert not (work / "baseline.hprof").exists()
    assert not (work / "final.hprof").exists()
    assert list(work.iterdir()) == []
