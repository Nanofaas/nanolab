"""Partial observations survive failures without unbounded in-memory buffering."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest


def writer_for(path, limit=65536):
    from nanolab.tasks.soak.artifacts import ArtifactWriter

    return ArtifactWriter(path, limit_bytes=limit)


def test_events_exist_before_writer_close(tmp_path):
    writer = writer_for(tmp_path)
    writer.append("events", {"phase": "drain"})
    assert json.loads((tmp_path / "events.jsonl").read_text()) == {"phase": "drain"}
    writer.close()


def test_existing_evidence_is_not_overwritten(tmp_path):
    evidence = tmp_path / "events.jsonl"
    evidence.write_text("important evidence\n")
    with pytest.raises(FileExistsError, match=r"run directory already contains"):
        writer_for(tmp_path)
    assert evidence.read_text() == "important evidence\n"


def test_two_writers_cannot_own_the_same_run(tmp_path):
    writer = writer_for(tmp_path)
    with pytest.raises(FileExistsError, match=r"run directory already contains"):
        writer_for(tmp_path)
    writer.close()
    with pytest.raises(FileExistsError, match=r"run directory already contains"):
        writer_for(tmp_path)


def test_quota_leaves_room_for_a_terminal_report(tmp_path):
    from nanolab.tasks.soak.artifacts import ArtifactLimitExceededError

    writer = writer_for(tmp_path, 1024)
    writer.append("samples", {"value": "a" * 750})
    with pytest.raises(ArtifactLimitExceededError, match="artifact budget exhausted"):
        writer.append("samples", {"value": "b" * 750})
    path = writer.write_json("terminal.json", {"status": "INCONCLUSIVE"})
    assert json.loads(path.read_text())["status"] == "INCONCLUSIVE"
    assert "b" * 750 not in (tmp_path / "samples.jsonl").read_text()
    writer.close()


def test_prior_evaluation_is_immutable(tmp_path):
    writer = writer_for(tmp_path)
    writer.write_json("evaluation-1.json", {"status": "INCONCLUSIVE"})
    with pytest.raises(FileExistsError, match="Errno 17"):
        writer.write_json("evaluation-1.json", {"status": "PASS"})
    writer.write_json("evaluation-2.json", {"status": "FAIL"})
    assert (
        json.loads((tmp_path / "evaluation-1.json").read_text())["status"]
        == "INCONCLUSIVE"
    )
    writer.close()


@pytest.mark.parametrize(
    "name", ["../outside", "/tmp/outside", "a/b", "a\\b", ".", ".."]
)
def test_stream_names_cannot_escape_the_run(tmp_path, name):
    writer = writer_for(tmp_path)
    with pytest.raises(ValueError, match=r"artifact name must be a single safe"):
        writer.append(name, {"value": 1})
    writer.close()


def test_symlink_target_cannot_redirect_writes(tmp_path):
    root = tmp_path / "run"
    outside = tmp_path / "outside.jsonl"
    outside.write_text("unchanged")
    writer = writer_for(root)
    (root / "events.jsonl").symlink_to(outside)
    with pytest.raises(OSError, match="Errno 40"):
        writer.append("events", {"value": 1})
    assert outside.read_text() == "unchanged"
    writer.close()


def test_concurrent_records_are_individually_readable(tmp_path):
    from nanolab.tasks.soak.artifacts import read_records

    writer = writer_for(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda value: writer.append("samples", {"value": value}), range(100)
            )
        )
    writer.close()
    records = list(read_records(tmp_path / "samples.jsonl"))
    assert sorted(record["value"] for record in records) == list(range(100))


def test_torn_final_record_keeps_the_valid_prefix_and_reports_a_gap(tmp_path):
    from nanolab.tasks.soak.artifacts import read_records

    path = tmp_path / "samples.jsonl"
    path.write_bytes(b'{"value": 1}\n{"value":')
    records = list(read_records(path))
    assert records[0] == {"value": 1}
    assert records[1]["kind"] == "observation_gap"
    assert records[1]["line"] == 2


def test_corrupt_complete_record_is_not_silently_skipped(tmp_path):
    from nanolab.tasks.soak.artifacts import ArtifactCorruptionError, read_records

    path = tmp_path / "samples.jsonl"
    path.write_text('{"value": 1}\ninvalid\n{"value": 3}\n')
    records = read_records(path)
    assert next(records) == {"value": 1}
    with pytest.raises(ArtifactCorruptionError, match="malformed record"):
        next(records)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_values_cannot_be_persisted_as_observations(tmp_path, value):
    writer = writer_for(tmp_path)
    with pytest.raises(ValueError, match="Out of range float values are not JSON c"):
        writer.append("samples", {"value": value})
    writer.close()


def test_disk_failure_is_visible_and_preserves_prior_records(tmp_path, monkeypatch):
    import errno
    import os

    writer = writer_for(tmp_path)
    writer.append("events", {"phase": "steady"})
    real_open = os.open

    def full_disk(path, flags, *args, **kwargs):
        if str(path).endswith("events.jsonl"):
            raise OSError(errno.ENOSPC, "disk full")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", full_disk)
    with pytest.raises(OSError, match="Errno 28"):
        writer.append("events", {"phase": "drain"})
    assert json.loads((tmp_path / "events.jsonl").read_text()) == {"phase": "steady"}
    writer.close()


def test_closed_writer_rejects_new_observations(tmp_path):
    writer = writer_for(tmp_path)
    writer.close()
    writer.close()
    with pytest.raises(RuntimeError, match="artifact writer is closed"):
        writer.append("events", {"phase": "drain"})


def test_fingerprint_is_canonical_and_sensitive_to_sdk_inputs():
    from nanolab.tasks.soak.artifacts import fingerprint

    assert fingerprint({"sdk": "a", "runtime": "node"}) == fingerprint(
        {"runtime": "node", "sdk": "a"}
    )
    assert fingerprint({"sdk": "a"}) != fingerprint({"sdk": "b"})


def test_large_artifact_identity_includes_size_and_sha256(tmp_path):
    from nanolab.tasks.soak.artifacts import describe_artifact

    path = tmp_path / "dump"
    path.write_bytes(b"abc")
    identity = describe_artifact(path)
    assert identity["size_bytes"] == 3
    assert (
        identity["sha256"]
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
