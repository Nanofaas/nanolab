"""Synthetic JFR destination regression; no Java or Docker target is run."""

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


def worker():
    path = Path(__file__).parents[2] / "assets/soak/diagnostic-worker.py"
    spec = importlib.util.spec_from_file_location("jfr_worker", path)
    module = importlib.util.module_from_spec(spec)  # pyright: ignore[reportArgumentType]
    spec.loader.exec_module(module)  # pyright: ignore[reportOptionalMemberAccess]
    return module


@pytest.mark.parametrize("body", [b"", b"not-a-recording"])
def test_zero_or_invalid_jfr_is_rejected_with_actual_command_output(
    tmp_path, monkeypatch, body
):
    module = worker()
    monkeypatch.setattr(
        module, "quota", lambda *args, **kwargs: {"capacity_bytes": 4096}
    )
    monkeypatch.setattr(
        module, "TemporaryDirectory", lambda **kwargs: TemporaryDirectory(dir=tmp_path)
    )

    def jcmd(args, deadline, scratch):
        Path(args[2].removeprefix("filename=")).write_bytes(body)
        return "Failed to stop recording. Could not set destination for recording."

    monkeypatch.setattr(module, "jcmd", jcmd)
    with pytest.raises(ValueError, match="JFR target file empty"):
        module.dump_jfr(
            {"quota_bytes": 4096}, "recording", tmp_path / "result.jfr", 100, tmp_path
        )
    assert not (tmp_path / "result.jfr").exists()


def test_jfr_uses_canonical_shared_tmp_and_keeps_verified_target_bytes(
    tmp_path, monkeypatch
):
    module = worker()
    monkeypatch.setattr(
        module, "quota", lambda *args, **kwargs: {"capacity_bytes": 4096}
    )

    def directory(**kwargs):
        assert kwargs["dir"] == "/tmp"
        return TemporaryDirectory(dir=tmp_path)

    monkeypatch.setattr(module, "TemporaryDirectory", directory)
    content = b"FLR\x00" + b"synthetic-runtime-bytes" * 4

    def jcmd(args, deadline, scratch):
        assert args[:2] == ("JFR.stop", "name=recording")
        destination = Path(args[2].removeprefix("filename="))
        assert not str(destination).startswith("/proc/")
        destination.write_bytes(content)
        return "Stopped recording, written to shared tmp"

    def command(args, deadline, scratch):
        assert args[:2] == ("/opt/java/openjdk/bin/jfr", "summary")
        assert Path(args[2]).read_bytes() == content
        return "Version: 2.1\nChunks: 1\n"

    monkeypatch.setattr(module, "jcmd", jcmd)
    monkeypatch.setattr(module, "command", command)
    output = tmp_path / "result.jfr"
    result = module.dump_jfr({"quota_bytes": 4096}, "recording", output, 100, tmp_path)
    assert output.read_bytes() == content
    assert result["target_write_bytes"] == len(content)
    assert "Chunks: 1" in result["summary_output"]


def test_shared_tmp_quota_failure_prevents_jfr_write(tmp_path, monkeypatch):
    module = worker()

    def quota(*args, **kwargs):
        assert kwargs["mountpoint"] == "/tmp"
        raise ValueError("output filesystem capacity exceeds reservation")

    monkeypatch.setattr(module, "quota", quota)
    monkeypatch.setattr(
        module, "jcmd", lambda *args: pytest.fail("must not write before quota check")
    )
    with pytest.raises(ValueError, match=r"output filesystem capacity exceeds"):
        module.dump_jfr(
            {"quota_bytes": 4096}, "recording", tmp_path / "result.jfr", 100, tmp_path
        )
