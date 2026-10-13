"""Exercise the actual pinned Python helpers through the saved simulation CLI."""

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def reference():
    root = os.environ.get("ONE_SHOT_REFERENCE_ROOT")
    python = os.environ.get("ONE_SHOT_REFERENCE_PYTHON")
    if not root or not python:
        pytest.skip("requires the isolated original Python reference environment")
    return root, python


def case(load: float = 7):
    functions = {
        node: {
            "f": {
                "serviceSeconds": 1,
                "utilization": 1,
                "memoryMiB": 1,
                "alpha": 1,
                "delta": 0.9,
                "gamma": 0.1,
            }
        }
        for node in ["a", "b", "c", "cloud"]
    }
    return {
        "id": "one-hop",
        "factor": 1,
        "context": {
            "edges": ["a", "b", "c"],
            "cloud": "cloud",
            "functions": ["f"],
            "parameters": functions,
            "pools": {"a": 1, "b": 4, "c": 1},
            "neighbors": {"a": ["b"], "b": ["a", "c"], "c": ["b"]},
            "cloudReplicas": {"f": 1},
            "flowQuantum": 1,
            "maxRounds": 8,
        },
        "rates": {
            node: {"f": load if node == "a" else 0} for node in ["a", "b", "c", "cloud"]
        },
    }


def run_case(tmp_path, reference, document):
    root, python = reference
    source, output = tmp_path / "case.json", tmp_path / "result.json"
    source.write_text(json.dumps(document))
    result = subprocess.run(
        [
            python,
            str(Path(__file__).parents[4] / "scripts/one-shot-tune-load.py"),
            "case",
            "--input",
            str(source),
            "--reference",
            root,
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return output, result


def test_actual_reference_conserves_flow_starts_replicas_and_uses_only_one_hop(
    tmp_path, reference
):
    output, process = run_case(tmp_path, reference, case())
    assert process.returncode == 0, process.stderr
    result = json.loads(output.read_text())
    assert len(result["rounds"]) == 8
    assert result["localUnits"] == [[1], [0], [0]]
    assert result["neighborUnits"][0][1] == [4]
    assert result["neighborUnits"][0][2] == [0]
    assert result["cloudRequestedUnits"] == [[2], [0], [0]]
    assert result["replicas"] == [[1], [4], [0]]
    assert result["rounds"][0]["additionalReplicas"][1] == [4]
    # Terminal capacity is an assessment, never proof of accepted executions.
    assert result["cloudAssessment"]["f"]["requestedRps"] == 2
    assert result["cloudAssessment"]["f"]["capacityExcessRps"] == 1
    for row in result["rounds"]:
        assert row["memoryUsedMiB"] == [1, 4, 0]


def test_actual_reference_accepts_zero_load_and_zero_replicas(tmp_path, reference):
    output, process = run_case(tmp_path, reference, case(load=0))
    assert process.returncode == 0, process.stderr
    result = json.loads(output.read_text())
    assert len(result["rounds"]) == 8
    assert result["replicas"] == [[0], [0], [0]]
    assert result["summary"]["offeredEdgeRps"] == 0
    assert result["summary"]["cloudRequestedEdgeRps"] == 0


def test_off_grid_load_fails_without_being_rounded_or_reported_as_infeasible(
    tmp_path, reference
):
    output, process = run_case(tmp_path, reference, case(load=1.23))
    assert process.returncode != 0
    assert "rate is not an exact multiple of flowQuantum" in process.stderr
    assert not output.exists()


@pytest.fixture
def tuner():
    import importlib.util

    path = Path(__file__).parents[4] / "scripts/one-shot-tune-load.py"
    spec = importlib.util.spec_from_file_location("one_shot_load_tuner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def predicted_routes(two_neighbors: bool = False, cloud_rps: float = 0):
    flows = [[[0, 0] for _ in range(3)] for _ in range(3)]
    flows[0][1] = [1, 2]
    if two_neighbors:
        flows[0][2] = [3, 0]
    return {
        "id": "test",
        "context": {
            "edges": ["a", "b", "c"],
            "functions": ["f", "g"],
            "flowQuantum": 0.05,
        },
        "neighborUnits": flows,
        "summary": {
            "neighborRps": 0.3 if two_neighbors else 0.15,
            "cloudRequestedEdgeRps": cloud_rps,
        },
    }


def test_route_coverage_counts_distinct_receivers_not_functions(tuner):
    result = predicted_routes()
    routes = tuner.peer_routes(result)
    assert len(routes) == 1
    assert routes[0]["source"] == "a"
    assert routes[0]["destination"] == "b"
    assert routes[0]["rateRps"] == pytest.approx(0.15)
    assert routes[0]["functions"] == {"f": 0.05, "g": 0.1}
    result["neighborUnits"][0][2] = [3, 0]
    routes = tuner.peer_routes(result)
    assert {route["destination"] for route in routes} == {"b", "c"}


def test_selection_allows_horizontal_only_at_endpoints(tuner):
    horizontal = predicted_routes()
    vertical = predicted_routes(cloud_rps=0.1)
    assert (
        tuner.select_candidate([horizontal, vertical], require_cloud=False)
        is horizontal
    )
    assert (
        tuner.select_candidate([horizontal, vertical], require_cloud=True) is vertical
    )
    with pytest.raises(ValueError, match="no candidate"):
        tuner.select_candidate([horizontal], require_cloud=True)


def test_selection_rejects_candidate_with_cloud_but_no_peer_flow(tuner):
    result = predicted_routes(cloud_rps=0.5)
    result["neighborUnits"][0][1] = [0, 0]
    # A summary alone is not proof of an actual route.
    with pytest.raises(ValueError, match="no candidate"):
        tuner.select_candidate([result], require_cloud=False)


def test_coverage_requires_nine_active_windows_and_two_multi_receiver_windows(tuner):
    selected = [predicted_routes(two_neighbors=e in {1, 2}) for e in range(9)]
    coverage = tuner.offload_coverage(selected)
    assert coverage["intervalsWithPeerOffload"] == 9
    assert coverage["intervalsWithMultipleNeighbors"] == 2
    assert coverage["intervals"][1]["originsWithMultipleNeighbors"] == ["a"]
    assert coverage["intervals"][0]["originsWithMultipleNeighbors"] == []
    selected[8]["neighborUnits"][0][1] = [0, 0]
    with pytest.raises(ValueError, match="every interval"):
        tuner.offload_coverage(selected)
    selected[8] = predicted_routes()
    selected[2] = predicted_routes()
    with pytest.raises(ValueError, match="two intervals"):
        tuner.offload_coverage(selected)
