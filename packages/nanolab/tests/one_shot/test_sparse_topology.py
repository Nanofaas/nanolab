"""Sparse peer identity must survive gossip and membership validation."""

from importlib import import_module

import pytest

from nanolab.config.one_shot import OneShotConfig


def settings(graph=None):
    ids = [f"edge-{n}" for n in range(1, 6)]
    return OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "scientific-experiment",
            "nodes": [{"id": node, "kind": "edge"} for node in ids]
            + [{"id": "cloud", "kind": "cloud"}],
            "functions": {"f": {"input": {}}},
            "neighbors": graph,
        }
    )


def graph():
    return {
        "edge-1": ["edge-2", "edge-5"],
        "edge-2": ["edge-1", "edge-3", "edge-5"],
        "edge-3": ["edge-2", "edge-4"],
        "edge-4": ["edge-3", "edge-5"],
        "edge-5": ["edge-1", "edge-2", "edge-4"],
    }


def test_declared_graph_excludes_non_neighbors():
    topology = import_module("nanolab.one_shot.topology")
    resolved = topology.validate_neighbor_graph(settings(graph()))
    assert [len(resolved[f"edge-{n}"]) for n in range(1, 6)] == [2, 3, 2, 2, 3]
    assert topology.operator_peer_modes("edge-1", resolved) == {
        "edge-2": "FORCE_ACTIVE",
        "edge-3": "EXCLUDED",
        "edge-4": "EXCLUDED",
        "edge-5": "FORCE_ACTIVE",
    }


@pytest.mark.parametrize(
    "change",
    ["self", "cloud", "unknown", "asymmetry", "disconnected", "duplicate", "missing"],
)
def test_invalid_neighbor_graph_is_rejected(change):
    topology = import_module("nanolab.one_shot.topology")
    data = graph()
    if change == "self":
        data["edge-1"].append("edge-1")
    if change == "cloud":
        data["edge-1"].append("cloud")
    if change == "unknown":
        data["edge-1"].append("edge-99")
    if change == "asymmetry":
        data["edge-1"].remove("edge-2")
    if change == "duplicate":
        data["edge-1"].append("edge-2")
    if change == "missing":
        data.pop("edge-1")
    if change == "disconnected":
        data = {
            "edge-1": ["edge-2"],
            "edge-2": ["edge-1"],
            "edge-3": ["edge-4", "edge-5"],
            "edge-4": ["edge-3", "edge-5"],
            "edge-5": ["edge-3", "edge-4"],
        }
    with pytest.raises(ValueError, match=r"neighbor|invalid"):
        topology.validate_neighbor_graph(settings(data))


def test_wrong_peer_with_same_count_and_drift_are_rejected():
    topology = import_module("nanolab.one_shot.topology")
    expected = topology.validate_neighbor_graph(settings(graph()))
    observed = {node: set(peers) for node, peers in expected.items()}
    topology.verify_neighbor_sets(observed, expected)
    observed["edge-1"] = {"edge-3", "edge-5"}
    with pytest.raises(ValueError, match="neighbor"):
        topology.verify_neighbor_sets(observed, expected)


def test_legacy_graph_remains_complete():
    topology = import_module("nanolab.one_shot.topology")
    assert topology.validate_neighbor_graph(settings())["edge-1"] == (
        "edge-2",
        "edge-3",
        "edge-4",
        "edge-5",
    )


@pytest.mark.parametrize("drift", [False, True])
def test_live_neighbors_include_rtt_and_reject_gossip_drift(drift):
    import httpx

    topology = import_module("nanolab.one_shot.topology")
    frozen = {"edge-1": ("edge-2",), "edge-2": ("edge-1",)}

    def respond(request):
        peer = "edge-2" if request.url.host == "edge-1" else "edge-1"
        if drift and request.url.host == "edge-1":
            peer = "edge-99"
        if request.url.path.endswith("/peers"):
            return httpx.Response(
                200,
                json=[
                    {"id": peer, "mode": "FORCE_ACTIVE", "active": True, "rttMs": 2.5}
                ],
            )
        return httpx.Response(
            200,
            json={
                "schemaVersion": 1,
                "state": "IDLE",
                "busy": False,
                "clockHealthy": True,
                "catalogGenerations": {},
                "localEndpoint": {},
                "revision": 0,
                "activePlan": {},
                "peerEndpoints": [
                    {
                        "peerId": peer,
                        "incarnation": "inc-1",
                        "invocationUri": "http://peer:8080",
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        if drift:
            with pytest.raises(ValueError, match="neighbor"):
                topology.observe_neighbors(
                    http, {node: "http://" + node for node in frozen}, frozen
                )
        else:
            result = topology.observe_neighbors(
                http, {node: "http://" + node for node in frozen}, frozen
            )
            assert result["edge-1"]["peers"][0]["rttMs"] == 2.5
            assert result["edge-2"]["endpoints"][0]["incarnation"] == "inc-1"
