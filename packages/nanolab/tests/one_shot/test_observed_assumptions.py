from types import SimpleNamespace
from typing import Any

from nanolab.tasks.one_shot.verification import validate_proof, verify_observations


def proof(**overrides):
    return dict(
        executionId="terminal",
        incarnation="runtime",
        dispatchAttempt="1",
        destination="cloud",
        origin="edge-0",
        function="work",
        state="RELEASED",
        handlerStarted=True,
        occupancySeconds=0.1,
        **overrides,
    )


def test_sdk_identity_must_match_queried_terminal_execution():
    assert validate_proof(
        {"executionId": "terminal", "incarnation": "runtime", "dispatchAttempt": "1"},
        "terminal",
    )
    assert not validate_proof(
        {"executionId": "other", "incarnation": "runtime", "dispatchAttempt": "1"},
        "terminal",
    )
    assert not validate_proof({"executionId": "terminal"}, "terminal")


def test_per_node_drift_cannot_cancel_and_hop_must_match_physical_destination():
    profile = SimpleNamespace(functions=[{"function": "work", "serviceSeconds": 0.1}])
    results = [{"originalId": "x", "executionNode": "edge-1", "origin": "edge-0"}]
    attempts = [
        {**proof(), "originalId": "x", "occupancySeconds": 0.15},
        {
            **proof(),
            "destination": "edge-0",
            "originalId": "y",
            "occupancySeconds": 0.05,
        },
    ]
    violations, observed = verify_observations(
        attempts,
        results,
        profile=profile,
        tolerance=0.35,
        nodes=["edge-0", "edge-1", "cloud"],
    )
    assert any("drift" in value for value in violations)
    assert any("destination" in value for value in violations)
    assert observed["cloud/work"]["drift"] > 0.35


def test_physical_inventory_rejects_concurrency_and_resource_drift():
    from nanolab.tasks.one_shot.verification import verify_runtime_inventory

    functions = {"work": SimpleNamespace(memory_mib=128, cpu=1)}
    row: dict[str, Any] = {
        "function": "work",
        "status": {
            "maxConcurrentHandlers": 1,
            "activeHandlers": 1,
            "physicalReleaseProof": True,
        },
        "container": {
            "HostConfig": {"Memory": 128 * 1024 * 1024, "NanoCpus": 1000000000}
        },
    }
    assert verify_runtime_inventory([row], functions=functions, nodes=[]) == []
    row["status"]["activeHandlers"] = 2
    row["container"]["HostConfig"]["Memory"] = 256 * 1024 * 1024
    violations = verify_runtime_inventory([row], functions=functions, nodes=[])
    assert len(violations) == 2


def test_ready_assignments_require_matching_peer_confirmation():
    import pytest

    from nanolab.tasks.one_shot.verification import verify_epoch_flows

    assignment = {
        "id": "a",
        "buyerId": "edge-0",
        "sellerId": "edge-1",
        "quantity": 1,
        "readyConfirmed": True,
    }
    function = {
        "forecastRate": 9,
        "localRate": 8,
        "cloudRate": 0,
        "outbound": [assignment],
        "inbound": [],
    }
    plans = {
        "edge-0": {"flowQuantum": 1, "functions": {"work": function}},
        "edge-1": {
            "flowQuantum": 1,
            "functions": {
                "work": {
                    "forecastRate": 0,
                    "localRate": 0,
                    "cloudRate": 0,
                    "outbound": [],
                    "inbound": [assignment],
                }
            },
        },
    }
    verify_epoch_flows(plans)
    plans["edge-1"]["functions"]["work"]["inbound"] = []
    with pytest.raises(ValueError, match="confirmation"):
        verify_epoch_flows(plans)


def test_realized_utility_uses_originals_and_declared_coefficients():
    from nanolab.tasks.one_shot.verification import realized_utility

    functions = {"work": SimpleNamespace(alpha=1, delta=0.9, gamma=0.1)}
    cells = [
        {
            "origin": "edge-0",
            "function": "work",
            "planned": 10,
            "destinations": {"edge-0": 4, "edge-1": 3, "cloud": 2},
            "errors": 1,
        }
    ]
    assert realized_utility(cells, functions=functions, cloud="cloud") == 0.64


def test_forwarding_target_must_equal_physical_terminal_url():
    profile = SimpleNamespace(functions=[{"function": "work", "serviceSeconds": 0.1}])
    attempts = [{**proof(), "originalId": "x"}]
    results = [
        {
            "originalId": "x",
            "origin": "edge-0",
            "executionNode": "cloud",
            "offloadedTarget": "http://edge-1:8080",
        }
    ]
    violations, _ = verify_observations(
        attempts,
        results,
        profile=profile,
        tolerance=0.35,
        nodes=["cloud"],
        endpoints={"cloud": "http://cloud:8080"},
    )
    assert any("extra hop" in value for value in violations)


def test_standard_baseline_without_node_header_uses_sdk_and_one_hop_target():
    profile = SimpleNamespace(functions=[{"function": "work", "serviceSeconds": 0.1}])
    attempts = [{**proof(), "originalId": "x"}]
    results = [
        {
            "originalId": "x",
            "origin": "edge-0",
            "executionNode": None,
            "offloadedTarget": "http://cloud",
        }
    ]
    kwargs: dict[str, Any] = {
        "profile": profile,
        "tolerance": 0.35,
        "nodes": ["edge-0", "cloud"],
        "endpoints": {"edge-0": "http://edge", "cloud": "http://cloud"},
    }
    assert (
        verify_observations(attempts, results, require_execution_node=False, **kwargs)[
            0
        ]
        == []
    )
    assert verify_observations(attempts, results, **kwargs)[0]
    results[0]["offloadedTarget"] = "http://other"
    assert verify_observations(
        attempts, results, require_execution_node=False, **kwargs
    )[0]
    results[0].update(offloadedTarget="http://cloud", executionNode="edge-0")
    assert verify_observations(
        attempts, results, require_execution_node=False, **kwargs
    )[0]
