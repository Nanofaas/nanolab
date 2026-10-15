"""Durable campaign rows have no presentation or operational side effects."""

import json
from pathlib import Path

import pytest


def test_recording_preserves_rows_and_atomically_replaces_status(tmp_path, capsys):
    from nanolab.one_shot.recording import CampaignRecorder

    recorder = CampaignRecorder(tmp_path)
    row = {"atUtc": "2026-10-15T12:00:00+00:00", "kind": "task.started"}
    progress = {"atUtc": row["atUtc"], "stage": "warmup", "epoch": 3}
    for _ in range(2):
        recorder.append_event(row)
        recorder.append_progress(progress)
    recorder.write_status({"state": "running"})
    with (tmp_path / "status.json").open() as old:
        recorder.write_status({"state": "completed", "atUtc": row["atUtc"]})
        assert json.load(old) == {"state": "running"}
    recorder.write_result({"state": "failed", "exitCode": 1})
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "completed"
    assert json.loads((tmp_path / "result.json").read_text())["exitCode"] == 1
    for name, expected in [("supervision.jsonl", row), ("progress.jsonl", progress)]:
        assert [json.loads(s) for s in (tmp_path / name).read_text().splitlines()] == [
            expected,
            expected,
        ]
    assert {p.name for p in tmp_path.iterdir()} == {
        "supervision.jsonl",
        "progress.jsonl",
        "status.json",
        "result.json",
    }
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "method", ["append_event", "append_progress", "write_status", "write_result"]
)
def test_recording_propagates_storage_errors(tmp_path, monkeypatch, method):
    from nanolab.one_shot.recording import CampaignRecorder

    def broken(*args, **kwargs):
        raise OSError("full storage")

    monkeypatch.setattr(Path, "open", broken)
    with pytest.raises(OSError, match="full storage"):
        getattr(CampaignRecorder(tmp_path), method)({"state": "running"})
