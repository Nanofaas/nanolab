"""Synthetic heapdump staging/evidence tests; no JVM or Docker execution."""

import base64
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from nanolab.workspace.paths import bundled_assets_root


def worker():
    """Load the script asset directly, independently of pytest import mode."""
    path = bundled_assets_root() / "soak/diagnostic-worker.py"
    spec = importlib.util.spec_from_file_location("heapdump_worker", path)
    module = importlib.util.module_from_spec(spec)  # pyright: ignore[reportArgumentType]
    spec.loader.exec_module(module)  # pyright: ignore[reportOptionalMemberAccess]
    return module


@pytest.fixture
def staging(tmp_path, monkeypatch):
    module = worker()
    cfg = {"quota_bytes": 16777216, "target": {"runtime": "jvm"}}
    request = {"max_bytes": 16777216, "request_id": "synthetic-request"}
    frames = []
    directories = []
    monkeypatch.setattr(module, "identity", lambda cfg: {})

    def quota(path, maximum, *, mountpoint):
        assert path == Path("/tmp") and mountpoint == "/tmp"
        assert maximum == 16777216
        return {"type": "tmpfs", "capacity_bytes": maximum}

    def directory(**kwargs):
        assert kwargs == {"prefix": "nanolab-heapdump-", "dir": "/tmp"}
        result = TemporaryDirectory(dir=tmp_path)
        directories.append(Path(result.name))
        return result

    monkeypatch.setattr(module, "quota", quota)
    monkeypatch.setattr(module, "TemporaryDirectory", directory)
    monkeypatch.setattr(module, "emit", frames.append)
    output = tmp_path / "output"
    output.mkdir()
    return module, cfg, request, output, frames, directories


def decode(frame):
    assert frame["name"] == "heapdump-command.json"
    return json.loads(base64.b64decode(frame["data_base64"]))


def test_shared_staging_and_evidence_counted_in_budget(staging, monkeypatch):
    module, cfg, request, output, frames, directories = staging
    content = b"JAVA PROFILE 1.0.2\x00" + b"synthetic" * 32

    def jcmd(args, deadline, scratch):
        assert args[0] == "GC.heap_dump" and len(args) == 2
        assert not args[1].startswith("/proc/")
        Path(args[1]).write_bytes(content)
        return f"Heap dump file created [{len(content)} bytes in 0.1 secs]"

    monkeypatch.setattr(module, "jcmd", jcmd)
    count = module.dump_heap_jvm(cfg, request, output, float("inf"), output)
    evidence = decode(frames[0])
    assert count == len(base64.b64decode(frames[0]["data_base64"]))
    assert evidence["request_id"] == request["request_id"]
    assert evidence["target"] == cfg["target"]
    assert evidence["target_write_bytes"] == len(content)
    assert evidence["argv"][:3] == ["/opt/java/openjdk/bin/jcmd", "1", "GC.heap_dump"]
    assert (output / "capture.hprof").read_bytes() == content
    assert all(not directory.exists() for directory in directories)


@pytest.mark.parametrize("mode", ["merge", "child", "header", "empty"])
def test_failure_keeps_command_evidence_without_exporting_dump(
    staging, monkeypatch, mode
):
    module, cfg, request, output, frames, directories = staging

    def jcmd(args, deadline, scratch):
        if mode == "child":
            raise RuntimeError("remote owned child failed: actual bounded excerpt")
        Path(args[1]).write_bytes(b"" if mode == "empty" else b"incomplete")
        if mode == "merge":
            return "Dump file is incomplete: Failed to merge segmented heap file"
        return "Heap dump file created [10 bytes in 0.1 secs]"

    monkeypatch.setattr(module, "jcmd", jcmd)
    with pytest.raises((ValueError, RuntimeError)):
        module.dump_heap_jvm(cfg, request, output, float("inf"), output)
    evidence = decode(frames[0])
    assert "error" in evidence
    assert "target_write_bytes" not in evidence
    if mode == "merge":
        assert "Failed to merge" in evidence["command_output"]
    if mode == "child":
        assert "actual bounded excerpt" in evidence["error"]
        assert "command_output" not in evidence
    assert not (output / "capture.hprof").exists()
    assert all(not directory.exists() for directory in directories)


def test_bad_shared_quota_prevents_command(staging, monkeypatch):
    module, cfg, request, output, frames, _ = staging

    def reject(*args, **kwargs):
        raise ValueError("output filesystem capacity exceeds reservation")

    monkeypatch.setattr(module, "quota", reject)
    monkeypatch.setattr(module, "jcmd", lambda *args: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match=r"output filesystem capacity exceeds"):
        module.dump_heap_jvm(cfg, request, output, float("inf"), output)
    assert frames == []


def test_command_output_is_bounded(staging, monkeypatch):
    module, cfg, request, output, frames, _ = staging
    monkeypatch.setattr(module, "jcmd", lambda *args: "x" * 100000)
    with pytest.raises(ValueError, match=r"JVM did not report a completed heap"):
        module.dump_heap_jvm(cfg, request, output, float("inf"), output)
    evidence = decode(frames[0])
    assert len(evidence["command_output"]) == 4096
    assert evidence["command_output_truncated"] is True
    assert len(base64.b64decode(frames[0]["data_base64"])) <= 16384
