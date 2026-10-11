"""Clock correlation remains observable without gating logical protocol v2."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from nanolab.tasks.one_shot.clock import clock_health_resource
from tests.one_shot.test_clock_scope import resources


@pytest.mark.parametrize("probe_fails", [False, True])
def test_v2_clock_diagnostics_keep_all_nodes_without_rejecting_load(
    tmp_path, monkeypatch, probe_fails
):
    endpoints, probes, inputs = resources()

    def measure(url, **kwargs):
        if probe_fails and "edge" in url:
            raise httpx.ConnectError("probe unavailable")
        return 30.0, 0.4, datetime.now(UTC)

    monkeypatch.setattr("nanolab.tasks.one_shot.clock.measure_http_clock", measure)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.clock.NanoFaasOneShotClient",
        lambda *_args, **_kwargs: pytest.fail("v2 must not depend on clock-health"),
    )
    owned = clock_health_resource(
        endpoints=endpoints,
        probes=probes,
        output=tmp_path / "clock.jsonl",
        protocol_version=2,
        interval_seconds=3600,
    )
    monitor = owned.acquire(inputs)
    try:
        monitor.require_healthy()
        assert monitor.probe_directory == tmp_path
    finally:
        monitor.close()
    rows = [
        json.loads(row) for row in (tmp_path / "clock.jsonl").read_text().splitlines()
    ]
    assert {row["node"] for row in rows} == {"edge", "cloud"}
    cloud = next(row for row in rows if row["node"] == "cloud")
    assert cloud["offsetSeconds"] == 30.0
    assert cloud["uncertaintySeconds"] == 0.4
    edge = next(row for row in rows if row["node"] == "edge")
    assert edge["status"] == ("MISSING" if probe_fails else "MEASURED")
    if probe_fails:
        assert edge["offsetSeconds"] is None
        assert edge["errorType"] == "ConnectError"


def test_v2_clock_evidence_write_failure_still_invalidates_collection(
    tmp_path, monkeypatch
):
    endpoints, probes, inputs = resources()
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.clock.measure_http_clock",
        lambda *_args, **_kwargs: (0.1, 0.01, datetime.now(UTC)),
    )
    owned = clock_health_resource(
        endpoints=endpoints,
        probes=probes,
        output=tmp_path / "missing-directory" / "clock.jsonl",
        protocol_version=2,
    )
    with pytest.raises(FileNotFoundError):
        owned.acquire(inputs)
