"""Partial observations survive failures without unbounded in-memory buffering."""

import json

import pytest


def writer_for(path, limit=65536):
    from nanolab.tasks.soak.artifacts import ArtifactWriter

    return ArtifactWriter(path, limit_bytes=limit)


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


def test_fingerprint_is_canonical_and_sensitive_to_sdk_inputs():
    from nanolab.tasks.soak.artifacts import fingerprint

    assert fingerprint({"sdk": "a", "runtime": "node"}) == fingerprint(
        {"runtime": "node", "sdk": "a"}
    )
    assert fingerprint({"sdk": "a"}) != fingerprint({"sdk": "b"})


def test_shared_storage_quota_failure_cannot_be_bypassed(tmp_path, monkeypatch):
    from sonata_tasks.artifacts import ArtifactWriter as SharedWriter

    from nanolab.tasks.soak.artifacts import ArtifactLimitExceededError

    writer = writer_for(tmp_path)

    def quota_failure(self, stream, record):
        raise ArtifactLimitExceededError("shared storage quota exhausted")

    monkeypatch.setattr(SharedWriter, "append", quota_failure)
    with pytest.raises(ArtifactLimitExceededError, match="shared storage quota"):
        writer.append("samples", {"value": 1})
    assert not (tmp_path / "samples.jsonl").exists()


def test_legacy_terminal_marker_and_gap_schema_are_preserved(tmp_path):
    from nanolab.tasks.soak.artifacts import read_records

    writer = writer_for(tmp_path, 1024)
    writer.append("samples", {"value": "a" * 750})
    with pytest.raises(OSError, match="artifact budget exhausted"):
        writer.write_json("summary.json", {"value": "b" * 150})
    writer.write_json("terminal.json", {"status": "INCONCLUSIVE"})
    assert (tmp_path / ".soak-owner").is_file()
    assert not (tmp_path / ".artifact-owner").exists()
    writer.close()
    path = tmp_path / "samples.jsonl"
    path.write_bytes(b'{"value":1}\n{"value":')
    assert list(read_records(path)) == [
        {"value": 1},
        {
            "schema": "nanolab-soak-v1",
            "kind": "observation_gap",
            "line": 2,
            "reason": "incomplete final record",
        },
    ]


def test_cumulative_writer_checks_direct_bytes_before_reserving_terminal_space(
    tmp_path,
):
    from nanolab.tasks.soak.artifacts import ArtifactLimitExceededError, ArtifactWriter

    (tmp_path / "direct.log").write_bytes(b"x" * 900)
    writer = ArtifactWriter(tmp_path / "evidence", 1024, budget_root=tmp_path)
    with pytest.raises(ArtifactLimitExceededError, match="budget exhausted"):
        writer.append("samples", {"value": 1})
    writer.write_json("terminal.json", {"status": "INCONCLUSIVE"})
    assert not (writer.root / "samples.jsonl").exists()
    assert (tmp_path / "direct.log").stat().st_size == 900


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ({"b": 2, "a": 1}, b'{"a":1,"b":2}'),
        ({"text": "é"}, b'{"text":"\\u00e9"}'),
        ({"rows": ({"x": 1.0},), "n": None}, b'{"n":null,"rows":[{"x":1.0}]}'),
        ({1: "one"}, b'{"1":"one"}'),
        (
            {"mapping": {2: "two", 10: "ten"}},
            b'{"mapping":{"2":"two","10":"ten"}}',
        ),
    ],
)
def test_fingerprint_preserves_canonical_json_bytes(value, canonical):
    import hashlib

    from nanolab.tasks.soak.artifacts import fingerprint

    assert fingerprint(value) == hashlib.sha256(canonical).hexdigest()


@pytest.mark.parametrize("value", [b"bytes", {"set"}, float("nan")])
def test_fingerprint_does_not_accept_additional_sonata_value_types(value):
    from nanolab.tasks.soak.artifacts import fingerprint

    with pytest.raises((TypeError, ValueError)):
        fingerprint({"value": value})
