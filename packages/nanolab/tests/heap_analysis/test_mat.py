"""Request validation, bounded docker argv, and manifest/status behavior.

No Docker daemon: MatAnalyzer.run() really execs a fake local "docker" stdlib
script (the same convention tests/soak/test_workload.py uses for k6), which
parses the "--mount ...target=/out" token from its own argv to find the
owned analysis directory and drops synthetic reports + a worker receipt
there, standing in for what a real container would have produced.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from nanolab.tasks.heap_analysis.mat import (
    _LOCK_PATH,
    REQUIRED_REPORTS,
    MatAnalysisRequest,
    MatAnalyzer,
    mat_run_argv,
)

HELPER = "example/mat-helper@sha256:" + "a" * 64

FAKE_DOCKER = """#!/usr/bin/env python3
import hashlib, json, os, sys
from pathlib import Path

argv = sys.argv[1:]
out_source = None
for index, token in enumerate(argv):
    if token == "--mount" and "target=/out" in argv[index + 1]:
        for part in argv[index + 1].split(","):
            if part.startswith("source="):
                out_source = Path(part.split("=", 1)[1])

calls = Path(sys.argv[0]).parent / "docker-calls.jsonl"
with calls.open('a') as stream:
    print(json.dumps(argv), file=stream)

if out_source is None:  # the teardown `docker rm --force` invocation
    sys.exit(0)

reports = out_source / "reports"
reports.mkdir(parents=True, exist_ok=True)

mode = os.environ.get("MAT_TEST_MODE", "ok")
required = json.loads(os.environ["MAT_TEST_REQUIRED"])
size = int(os.environ.get("MAT_TEST_REPORT_BYTES", "4096"))
invocations = []
for dump, report_id in required:
    if mode == "missing" and report_id == "org.eclipse.mat.api:suspects2":
        continue
    name = dump + "-" + report_id.split(":")[-1] + ".zip"
    content = os.urandom(size)
    (reports / name).write_bytes(content)
    if mode == "unreadable" and report_id == "org.eclipse.mat.api:suspects2":
        os.chmod(reports / name, 0)
    invocations.append(
        {
            "dump": dump,
            "report_id": report_id,
            "argv": ["ParseHeapDump.sh", dump + ".hprof", report_id],
            "returncode": 0,
            "outputs": [
                {
                    "name": name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            ],
        }
    )
(out_source / "mat-worker-receipt.json").write_text(
    json.dumps({"invocations": invocations})
)
sys.exit(1 if mode == "crash" else 0)
"""


def _install_fake_docker(tmp_path):
    path = tmp_path / "docker"
    path.write_text(FAKE_DOCKER)
    path.chmod(0o755)
    return path


def _request(tmp_path, **overrides):
    baseline = tmp_path / "baseline.hprof"
    baseline.write_bytes(b"baseline-bytes")
    final = tmp_path / "final.hprof"
    final.write_bytes(b"final-bytes")
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    values = {
        "baseline_hprof": baseline,
        "final_hprof": final,
        "output_dir": output_dir,
        "helper_image": HELPER,
        "artifact_limit_bytes": 10_000_000,
        "mat_memory_mib": 2048,
        "mat_cpus": 2.0,
        "mat_timeout_s": 30,
        "uid": 1000,
        "gid": 1000,
        "docker": "/usr/bin/docker",
        "docker_host": "unix:///var/run/docker.sock",
    }
    values.update(overrides)
    return MatAnalysisRequest(**values)


# --- MatAnalysisRequest rejections ------------------------------------------


def test_rejects_a_tagged_helper_image(tmp_path):
    with pytest.raises(ValueError, match="digest-pinned"):
        _request(tmp_path, helper_image="example/mat-helper:latest")


def test_rejects_a_missing_dump(tmp_path):
    baseline = tmp_path / "baseline.hprof"
    baseline.write_bytes(b"baseline-bytes")
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    with pytest.raises(ValueError, match="missing"):
        MatAnalysisRequest(
            baseline_hprof=baseline,
            final_hprof=tmp_path / "does-not-exist.hprof",
            output_dir=output_dir,
            helper_image=HELPER,
            artifact_limit_bytes=10_000_000,
            mat_memory_mib=2048,
            mat_cpus=2.0,
            mat_timeout_s=30,
            uid=1000,
            gid=1000,
        )


def test_accepts_any_dump_pair_that_was_actually_captured(tmp_path):
    """Dump size is deliberately unbounded: see the module docstring in mat.py.

    A dump that was captured is already on disk, so refusing to analyze it
    after the fact throws away the whole run and protects nothing.
    """
    request = _request(tmp_path)
    request.baseline_hprof.write_bytes(b"x" * 1_000_000)
    assert "max_dump_bytes" not in vars(request)
    assert mat_run_argv(request)


def test_rejects_a_symlinked_dump(tmp_path):
    real = tmp_path / "real.hprof"
    real.write_bytes(b"baseline-bytes")
    link = tmp_path / "baseline.hprof"
    link.symlink_to(real)
    final = tmp_path / "final.hprof"
    final.write_bytes(b"final-bytes")
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    with pytest.raises(ValueError, match="symlink"):
        MatAnalysisRequest(
            baseline_hprof=link,
            final_hprof=final,
            output_dir=output_dir,
            helper_image=HELPER,
            artifact_limit_bytes=10_000_000,
            mat_memory_mib=2048,
            mat_cpus=2.0,
            mat_timeout_s=30,
            uid=1000,
            gid=1000,
        )


# --- mat_run_argv bounds -----------------------------------------------------


def test_argv_is_bounded_and_mounts_are_correctly_scoped(tmp_path):
    request = _request(tmp_path)
    argv = mat_run_argv(request)
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert "--cpus" in argv and argv[argv.index("--cpus") + 1] == "2.0"
    assert "--memory" in argv and argv[argv.index("--memory") + 1] == "2048m"
    mounts = [argv[i + 1] for i, token in enumerate(argv) if token == "--mount"]
    baseline_mount = next(m for m in mounts if "target=/dumps/baseline.hprof" in m)
    final_mount = next(m for m in mounts if "target=/dumps/final.hprof" in m)
    out_mount = next(m for m in mounts if "target=/out" in m)
    assert f"source={request.baseline_hprof}" in baseline_mount
    assert baseline_mount.endswith(",readonly")
    assert f"source={request.final_hprof}" in final_mount
    assert final_mount.endswith(",readonly")
    assert f"source={request.analysis_dir}" in out_mount
    assert not out_mount.endswith(",readonly")
    work_mount = next(m for m in mounts if m.endswith("target=/work"))
    assert work_mount == f"type=bind,source={request.work_dir},target=/work"
    # /work is disk-backed and uncapped on purpose: a tmpfs charges its pages
    # to this container's memory cgroup and is OOM-killed against --memory
    # long before any size= quota could bind.
    tmpfs = [argv[i + 1] for i, token in enumerate(argv) if token == "--tmpfs"]
    assert not any(entry.startswith("/work") for entry in tmpfs)


def test_argv_names_and_labels_the_container_so_it_can_always_be_reaped(tmp_path):
    """A nameless MAT survives the run: SIGKILL on the CLI never reaches it."""
    request = _request(tmp_path)
    argv = mat_run_argv(request)
    assert argv[argv.index("--name") + 1] == request.container_name
    assert request.container_name == f"nanolab-mat-{request.output_dir.name}"
    labels = [argv[i + 1] for i, token in enumerate(argv) if token == "--label"]
    assert f"nanolab.run={request.output_dir.name}" in labels
    assert "nanolab.mat=true" in labels
    assert "--pull=never" in argv
    assert argv[argv.index("--ipc") + 1] == "private"
    assert argv[argv.index("--cgroupns") + 1] == "private"


def test_argv_gives_mat_a_writable_home_and_java_tmpdir(tmp_path):
    """A real run demonstrated a crash caused by missing writable mounts.

    On a `--read-only` container with no
    HOME set, Eclipse's launcher tries to write its configuration area
    under `/.eclipse/...` and fails with "Invalid Configuration Location"
    (exit 15). Even with HOME fixed, MAT's report renderer still calls
    `File.createTempFile` against Java's default `java.io.tmpdir` (`/tmp`),
    which does not exist as a writable mount either, and fails with
    `IOException: Read-only file system`. Both were reproduced against the
    real pinned helper image; every real MAT invocation failed until both
    were fixed.
    """
    request = _request(tmp_path)
    argv = mat_run_argv(request)
    assert "--env" in argv and "HOME=/work" in argv
    tmpfs = [argv[i + 1] for i, token in enumerate(argv) if token == "--tmpfs"]
    assert any(entry.startswith("/tmp:rw,") for entry in tmpfs), (
        "MAT needs a writable /tmp for java.io.tmpdir; only /work was mounted"
    )


# --- MatAnalyzer.run() behavior ----------------------------------------------


def _run_with_fake_docker(tmp_path, *, mode="ok", report_bytes=4096, **overrides):
    docker = _install_fake_docker(tmp_path)
    request = _request(tmp_path, docker=str(docker), **overrides)
    os.environ["MAT_TEST_MODE"] = mode
    os.environ["MAT_TEST_REQUIRED"] = json.dumps(list(REQUIRED_REPORTS))
    os.environ["MAT_TEST_REPORT_BYTES"] = str(report_bytes)
    try:
        manifest_path = MatAnalyzer().run(request)
    finally:
        for key in ("MAT_TEST_MODE", "MAT_TEST_REQUIRED", "MAT_TEST_REPORT_BYTES"):
            os.environ.pop(key, None)
    return manifest_path, request


def test_run_produces_a_pass_manifest_binding_reports_to_dump_hashes(tmp_path):
    manifest_path, request = _run_with_fake_docker(tmp_path)
    assert manifest_path == request.analysis_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "PASS"
    assert len(manifest["reports"]) == len(REQUIRED_REPORTS)
    baseline_digest = hashlib.sha256(request.baseline_hprof.read_bytes()).hexdigest()
    assert manifest["dumps"]["baseline"]["sha256"] == baseline_digest
    for entry in manifest["reports"].values():
        for described in entry["files"]:
            path = Path(described["path"])
            assert described["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest["errors"] == []


def test_run_reports_non_pass_status_when_a_required_report_is_missing(tmp_path):
    manifest_path, _request = _run_with_fake_docker(tmp_path, mode="missing")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] != "PASS"
    assert any("suspects2" in error for error in manifest["errors"])


def test_run_reports_non_pass_status_when_reports_exceed_the_output_budget(tmp_path):
    manifest_path, _request = _run_with_fake_docker(
        tmp_path, report_bytes=2_000_000, artifact_limit_bytes=1_000_000
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] != "PASS"
    assert any("budget" in error or "exceed" in error for error in manifest["errors"])


def test_run_reports_non_pass_status_when_the_container_exits_nonzero(tmp_path):
    manifest_path, _request = _run_with_fake_docker(tmp_path, mode="crash")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] != "PASS"


def test_run_refuses_to_reuse_an_existing_analysis_directory(tmp_path):
    docker = _install_fake_docker(tmp_path)
    request = _request(tmp_path, docker=str(docker))
    request.analysis_dir.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        MatAnalyzer().run(request)


def test_run_manifest_records_the_mat_version_from_the_lock_file(tmp_path):
    manifest_path, _request = _run_with_fake_docker(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    lock = json.loads(_LOCK_PATH.read_text())
    assert manifest["mat_version"] == lock["version"]
    assert manifest["mat_version"] != "unknown"


def test_run_degrades_to_non_pass_instead_of_raising_on_an_unreadable_report(
    tmp_path,
):
    # A report can become unreadable between being written and being hashed
    # (a permission race, a truncated write, disk pressure). run() must still
    # publish a manifest rather than let describe_artifact's OSError escape.
    manifest_path, request = _run_with_fake_docker(tmp_path, mode="unreadable")
    try:
        manifest = json.loads(manifest_path.read_text())
        assert manifest["status"] != "PASS"
        assert any(
            "unreadable" in error and "suspects2" in error
            for error in manifest["errors"]
        )
    finally:
        broken = request.analysis_dir / "reports" / "final-suspects2.zip"
        broken.chmod(0o644)


def _docker_calls(tmp_path):
    path = tmp_path / "docker-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_run_always_reaps_the_named_container_and_removes_the_work_area(tmp_path):
    """The mat_timeout_s path SIGKILLs the docker CLI, never the container."""
    _manifest, request = _run_with_fake_docker(tmp_path)
    removals = [
        call for call in _docker_calls(tmp_path) if call[2:4] == ["rm", "--force"]
    ]
    assert removals and removals[-1][-1] == request.container_name
    assert not request.work_dir.exists()


def test_run_reaps_the_container_even_when_the_run_raises(tmp_path, monkeypatch):
    from nanolab.tasks.heap_analysis import mat as mat_module

    seen: list[tuple[str, ...]] = []

    def fake_run_owned_command(argv, **_kwargs):
        seen.append(tuple(argv))
        if "rm" in argv:
            raise RuntimeError("reap also failed; must not mask the real error")
        raise RuntimeError("boom")

    monkeypatch.setattr(mat_module, "run_owned_command", fake_run_owned_command)
    request = _request(tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        MatAnalyzer().run(request)
    assert any("rm" in argv and request.container_name in argv for argv in seen)
    assert not request.work_dir.exists()
