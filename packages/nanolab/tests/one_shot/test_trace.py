from datetime import UTC, datetime

import pytest

from nanolab.tasks.one_shot.trace import TraceArtifact, oracle_entries, request_schedule


def trace(rate: float = 4, quantum: float = 1):
    return TraceArtifact(
        nodes=["edge-0", "edge-1"],
        functions=["work"],
        period_seconds=20,
        flow_quantum=quantum,
        seed=7,
        payloads={"work": {"seed": 7}},
        windows=[{"edge-0": {"work": rate}, "edge-1": {"work": 2}}],
    )


def test_same_trace_produces_matching_oracle_and_schedule():
    original = trace()
    anchor = datetime(2026, 10, 9, tzinfo=UTC)
    schedule = request_schedule(
        original,
        anchor=anchor,
        endpoints={"edge-0": "http://e0", "edge-1": "http://e1"},
        run_id="run",
    )
    for node in original.nodes:
        entries = oracle_entries(
            original, node=node, generations={"work": 1}, anchor=anchor
        )
        rows = [row for row in schedule if row["origin"] == node]
        assert len(rows) == entries[0]["rate"] * 20
        assert len({row["originalId"] for row in rows}) == len(rows)
        assert all(row["scheduledAt"] >= anchor.timestamp() for row in rows)
    assert original.sha256 == trace().sha256
    assert schedule == request_schedule(
        original,
        anchor=anchor,
        endpoints={"edge-0": "http://e0", "edge-1": "http://e1"},
        run_id="run",
    )


def test_oracle_remainder_and_incomplete_function_scope_are_rejected():
    with pytest.raises(ValueError, match="representable"):
        trace(rate=4.5)
    with pytest.raises(ValueError, match="scope"):
        TraceArtifact.model_validate(
            {**trace().model_dump(), "windows": [{"edge-0": {"work": 4}}]}
        )
