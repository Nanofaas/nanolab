"""Measure terminal clocks without invoking disabled negotiation endpoints."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sonata_engine import Resource, TaskInputs

from nanolab.tasks.one_shot.clock import clock_health_resource


def resources():
    endpoints = {
        node: Resource(title=node, acquire=lambda _: "", release=lambda *_: None)
        for node in ("edge", "cloud")
    }
    probes = {
        node: Resource(
            title=node + " probe", acquire=lambda _: "", release=lambda *_: None
        )
        for node in endpoints
    }
    values = {value: "http://" + node for node, value in endpoints.items()}
    values.update(
        {value: "http://" + node + "/clock" for node, value in probes.items()}
    )
    return endpoints, probes, TaskInputs._for_resources(values, set(values))


@pytest.mark.parametrize("reporting_nodes", [frozenset({"edge"}), frozenset()])
def test_all_nodes_measured_but_only_configured_nodes_receive_health(
    tmp_path, monkeypatch, reporting_nodes
):
    endpoints, probes, inputs = resources()
    publications = []
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.clock.measure_http_clock",
        lambda *args, **kw: (0.002, 0.001, datetime.now(UTC)),
    )

    def client(url, **kw):
        assert url != "http://cloud"
        return SimpleNamespace(
            update_clock_health=lambda **kwargs: (
                publications.append(url) or SimpleNamespace(healthy=True)
            )
        )

    monkeypatch.setattr("nanolab.tasks.one_shot.clock.NanoFaasOneShotClient", client)
    owned = clock_health_resource(
        endpoints=endpoints,
        probes=probes,
        output=tmp_path / "clock.jsonl",
        reporting_nodes=reporting_nodes,
        interval_seconds=3600,
    )
    monitor = owned.acquire(inputs)
    monitor.close()
    rows = [
        json.loads(line) for line in (tmp_path / "clock.jsonl").read_text().splitlines()
    ]
    assert {row["node"] for row in rows} == {"edge", "cloud"}
    assert publications == (["http://edge"] if reporting_nodes else [])


@pytest.mark.parametrize(("offset", "uncertainty"), [(0.2, 0.001), (0.09, 0.02)])
def test_nonreporting_cloud_still_fails_an_unhealthy_clock(
    tmp_path, monkeypatch, offset, uncertainty
):
    endpoints, probes, inputs = resources()
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.clock.measure_http_clock",
        lambda url, **kw: (
            offset if "cloud" in url else 0,
            uncertainty,
            datetime.now(UTC),
        ),
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.clock.NanoFaasOneShotClient",
        lambda *args, **kw: SimpleNamespace(
            update_clock_health=lambda **kwargs: SimpleNamespace(healthy=True)
        ),
    )
    owned = clock_health_resource(
        endpoints=endpoints,
        probes=probes,
        output=tmp_path / "clock.jsonl",
        reporting_nodes={"edge"},
    )
    with pytest.raises(ValueError, match="unhealthy or too uncertain"):
        owned.acquire(inputs)
    assert "cloud" in (tmp_path / "clock.jsonl").read_text()


def test_enabled_edge_http_failure_is_not_ignored(tmp_path, monkeypatch):
    endpoints, probes, inputs = resources()
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.clock.measure_http_clock",
        lambda *args, **kw: (0, 0.001, datetime.now(UTC)),
    )

    def publish(**kw):
        request = httpx.Request("PUT", "http://edge/clock-health")
        raise httpx.HTTPStatusError(
            "missing edge endpoint",
            request=request,
            response=httpx.Response(404, request=request),
        )

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.clock.NanoFaasOneShotClient",
        lambda *args, **kw: SimpleNamespace(update_clock_health=publish),
    )
    owned = clock_health_resource(
        endpoints=endpoints,
        probes=probes,
        output=tmp_path / "clock.jsonl",
        reporting_nodes={"edge"},
    )
    with pytest.raises(httpx.HTTPStatusError):
        owned.acquire(inputs)


def test_unknown_reporting_node_fails_before_acquisition(tmp_path):
    endpoints, probes, _inputs = resources()
    with pytest.raises(ValueError, match=r"reporting.*scope"):
        clock_health_resource(
            endpoints=endpoints,
            probes=probes,
            output=tmp_path / "clock.jsonl",
            reporting_nodes={"missing"},
        )


def test_rejected_clock_keeps_all_raw_probe_attempts(tmp_path, monkeypatch):
    """An inconclusive clock must retain all HTTP samples, not only its minimum."""
    import time

    endpoints, probes, inputs = resources()
    client_class = httpx.Client
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.clock.httpx.Client",
        lambda **kwargs: client_class(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"timestamp": time.time() + 1})
            ),
            **kwargs,
        ),
    )
    owned = clock_health_resource(
        endpoints=endpoints,
        probes=probes,
        output=tmp_path / "clock.jsonl",
        reporting_nodes=set(),
    )
    with pytest.raises(ValueError, match="unhealthy or too uncertain"):
        owned.acquire(inputs)
    rows = [
        json.loads(line)
        for line in (tmp_path / "clock-probes.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 3
    assert {row["node"] for row in rows} == {"edge"}
    assert [row["attempt"] for row in rows] == [0, 1, 2]
    assert all(row["remoteUtcSeconds"] > row["afterUtcSeconds"] for row in rows)
    assert all(row["errorType"] is None for row in rows)
    assert (tmp_path / "clock.jsonl").exists()
