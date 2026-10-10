"""Keep native calibration and independent accounting specific to each node."""

from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nanolab.config.one_shot import ProtocolSettings, TimingCell, resolve_node_functions
from nanolab.one_shot.models import CalibrationProfile
from nanolab.tasks.one_shot.trace import TraceArtifact
from nanolab.tasks.one_shot.verification import (
    verify_epoch_flows,
    verify_observations,
    verify_runtime_inventory,
)
from tests.one_shot.test_node_profiles import fixture, read


def profiles(tmp_path):
    data, index = fixture(tmp_path)
    result = read(tmp_path, data, index)
    from nanolab.config.one_shot import OneShotConfig

    return OneShotConfig.model_validate(data), result


def observation(node, demand, *, header=True):
    proof = {
        "originalId": node,
        "function": "f",
        "destination": node,
        "executionId": node,
        "incarnation": "runtime-1",
        "dispatchAttempt": 1,
        "state": "RELEASED",
        "handlerStarted": True,
        "occupancySeconds": demand,
    }
    result = {
        "originalId": node,
        "origin": node,
        "executionId": node,
        "terminalExecutionId": node,
        "executionNode": node if header else None,
    }
    return proof, result


def test_destination_service_uses_local_demand_and_tolerance(tmp_path):
    _settings, profile = profiles(tmp_path)
    proof1, row1 = observation("edge-1", 0.30)
    proof2, row2 = observation("cloud", 0.10)
    violations, observed = verify_observations(
        [proof1, proof2],
        [row1, row2],
        profile=profile,
        tolerance=0.2,
        nodes=list(profile.by_node),
    )
    assert violations == []
    assert observed["edge-1/f"]["drift"] == 0
    assert observed["cloud/f"]["drift"] == 0
    profile.by_node["edge-1"].functions[0]["validity"]["maxRelativeCapacityError"] = (
        0.01
    )
    proof1["occupancySeconds"] = 0.32
    violations, _ = verify_observations(
        [proof1, proof2],
        [row1, row2],
        profile=profile,
        tolerance=0.2,
        nodes=list(profile.by_node),
    )
    assert any("edge-1/f" in v for v in violations)


@pytest.mark.parametrize("bad", [None, "identity", "header", "edge"])
def test_cloud_direct_standard_api_requires_exact_local_sdk_proof(tmp_path, bad):
    _settings, profile = profiles(tmp_path)
    proof, row = observation("cloud", 0.1, header=False)
    if bad == "identity":
        proof["executionId"] = "other"
    elif bad == "header":
        row["executionNode"] = "edge-1"
    elif bad == "edge":
        row["origin"] = "edge-1"
    violations, _ = verify_observations(
        [proof],
        [row],
        profile=profile,
        tolerance=0.2,
        nodes=list(profile.by_node),
        cloud="cloud",
    )
    assert bool(violations) == (bad is not None)


def test_configuration_uses_own_profile_identity_and_effective_limits(tmp_path):
    settings, profile = profiles(tmp_path)
    module = import_module("nanolab.tasks.one_shot.qualification")
    topology = SimpleNamespace(
        function_settings=settings.functions,
        node_function_settings={
            n.id: resolve_node_functions(settings, n.id) for n in settings.nodes
        },
        resources=SimpleNamespace(
            nodes={n.id: SimpleNamespace(config=n) for n in settings.nodes}
        ),
    )
    client = SimpleNamespace(
        status=lambda: SimpleNamespace(catalog_generations={"f": 1})
    )
    configurations = [
        module.configuration_for_node(
            topology,
            settings,
            profile,
            n,
            client=client,
            cloud_uri="http://cloud",
            period=20,
            lead=2,
            epsilon=0.1,
            protocol=ProtocolSettings(),
            anchor=datetime.now(UTC),
        )
        for n in ["edge-1", "edge-2"]
    ]
    assert configurations[0]["profileId"] != configurations[1]["profileId"]
    assert (
        configurations[0]["environmentFingerprint"]
        != configurations[1]["environmentFingerprint"]
    )


@pytest.mark.parametrize("swap", [False, True])
def test_scope_revalidates_each_node_identity_and_resource_binding(
    tmp_path, monkeypatch, swap
):
    settings, profile = profiles(tmp_path)
    module = import_module("nanolab.tasks.one_shot.qualification")
    expected = {n: p.environment_fingerprint for n, p in profile.by_node.items()}
    monkeypatch.setattr(
        module, "environment_identity", lambda *_: (profile.environment_fingerprint, {})
    )
    monkeypatch.setattr(
        module,
        "node_environment_identity",
        lambda _s, _p, _b, n: (expected[n], {}),
        raising=False,
    )
    topology = SimpleNamespace(
        function_settings=settings.functions,
        node_function_settings={
            n.id: resolve_node_functions(settings, n.id) for n in settings.nodes
        },
    )
    image = profile.by_node["edge-1"].functions[0]["imageDigest"]
    built = SimpleNamespace(
        function=lambda *_: SimpleNamespace(image=SimpleNamespace(id=image))
    )
    if swap:
        profile.by_node["edge-1"], profile.by_node["cloud"] = (
            profile.by_node["cloud"],
            profile.by_node["edge-1"],
        )
        with pytest.raises(ValueError, match="mismatch"):
            module.verify_profile_scope(profile, settings, topology, None, built)
    else:
        assert (
            module.verify_profile_scope(profile, settings, topology, None, built)
            == profile.environment_fingerprint
        )


def test_inventory_uses_destination_quota(tmp_path):
    settings, _profile = profiles(tmp_path)
    functions = {n.id: resolve_node_functions(settings, n.id) for n in settings.nodes}
    rows = [
        {
            "node": "edge-1",
            "at": "now",
            "function": "f",
            "status": {
                "maxConcurrentHandlers": 1,
                "activeHandlers": 0,
                "physicalReleaseProof": True,
            },
            "container": {
                "HostConfig": {"Memory": 128 * 1024 * 1024, "NanoCpus": 350000000}
            },
        }
    ]
    nodes = {n.id: SimpleNamespace(config=n) for n in settings.nodes}
    assert (
        verify_runtime_inventory(
            rows, functions=settings.functions, node_functions=functions, nodes=nodes
        )
        == []
    )
    rows[0]["container"]["HostConfig"]["NanoCpus"] = 1000000000
    assert verify_runtime_inventory(
        rows, functions=settings.functions, node_functions=functions, nodes=nodes
    )


def test_qualification_requires_actual_vector_not_total_or_independent_maxima():
    module = import_module("nanolab.tasks.one_shot.experiment")
    trace = TraceArtifact(
        nodes=["edge-1", "edge-2", "cloud"],
        functions=["f"],
        period_seconds=10,
        flow_quantum=1,
        seed=7,
        payloads={"f": {}},
        windows=[{"edge-1": {"f": 8}, "edge-2": {"f": 2}, "cloud": {"f": 1}}],
    )
    qualification = SimpleNamespace(
        matrix=[
            {
                "cell": {
                    "id": "other",
                    "rates": {
                        "edge-1": {"f": 2},
                        "edge-2": {"f": 8},
                        "cloud": {"f": 1},
                    },
                }
            }
        ]
    )
    with pytest.raises(ValueError, match="vector"):
        module.verify_qualified_load(trace, qualification)
    qualification.matrix.append({"cell": {"id": "actual", "rates": trace.windows[0]}})
    module.verify_qualified_load(trace, qualification)


@pytest.mark.parametrize("bad", [None, "excluded", "reciprocal"])
def test_peer_flows_require_declared_arc_and_reciprocal_receipt(bad):
    row = {
        "buyerId": "edge-1",
        "sellerId": "edge-2",
        "quantity": 1,
        "readyConfirmed": True,
    }
    plans = {
        "edge-1": {
            "flowQuantum": 1,
            "functions": {
                "f": {
                    "forecastRate": 2,
                    "localRate": 1,
                    "cloudRate": 0,
                    "outbound": [row],
                    "inbound": [],
                }
            },
        },
        "edge-2": {
            "flowQuantum": 1,
            "functions": {
                "f": {
                    "forecastRate": 1,
                    "localRate": 1,
                    "cloudRate": 0,
                    "outbound": [],
                    "inbound": [row],
                }
            },
        },
    }
    graph = {"edge-1": ["edge-2"], "edge-2": ["edge-1"]}
    if bad == "excluded":
        graph = {"edge-1": [], "edge-2": []}
    elif bad == "reciprocal":
        plans["edge-2"]["functions"]["f"]["inbound"] = []
    if bad:
        with pytest.raises(ValueError, match=r"topology|reciprocal"):
            verify_epoch_flows(plans, neighbor_graph=graph)
    else:
        verify_epoch_flows(plans, neighbor_graph=graph)


def test_qualification_scales_terminal_cloud_own_pool_and_observes_cloud_origin(
    tmp_path, monkeypatch
):
    from contextlib import nullcontext

    import httpx

    from nanolab.one_shot.profiles import CalibrationProfiles
    from tests.one_shot.test_qualification_task import task_fixture

    task, inputs, calls = task_fixture(tmp_path, monkeypatch)
    original_inputs = inputs
    inputs = SimpleNamespace(
        resource=lambda key: (
            original_inputs.resource(key) if key == "clock" else "http://" + key
        ),
        upstream=original_inputs.upstream,
    )
    native = task.profile
    assert isinstance(native, CalibrationProfile)
    assert task.settings.timing is not None
    task.settings.purpose = "scientific-experiment"
    for cell in task.settings.timing.matrix:
        cell.rates["cloud"] = {"one-shot-workload-rust": 1.0}
    task.profile = CalibrationProfiles(
        by_node={n.id: native for n in task.settings.nodes},
        sha256="a" * 64,
        environment_fingerprint=native.environment_fingerprint,
        capacities={n.id: 10.0 for n in task.settings.nodes},
    )
    task.topology.node_function_settings = {
        n.id: {
            "one-shot-workload": task.settings.functions[
                "one-shot-workload-rust"
            ].model_copy(update={"max_replicas": 1 if n.id == "cloud" else 2})
        }
        for n in task.settings.nodes
    }
    actions = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (
                actions.append(request)
                or httpx.Response(
                    200, json={"maxConcurrentHandlers": 1, "physicalReleaseProof": True}
                )
            )
        )
    ) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.qualification.httpx.Client",
            lambda **_: nullcontext(http),
        )
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.qualification.runtime_endpoint",
            lambda *_a, **_k: ("http://sdk", {}),
        )
        result = task.run(cast(Any, inputs)).value
    assert result and result.qualified
    assert all(
        "cloud" in TimingCell.model_validate(cell["cell"]).rates
        for cell in result.matrix
    )
    assert len([row for row in calls if row[1] == "profile"]) == 2
    assert len([row for row in actions if row.method == "PUT"]) == 1
    assert len([row for row in actions if row.method == "GET"]) == 1
    import json

    assert json.loads(actions[0].content)["replicas"] == 1


def test_legacy_reader_cannot_bypass_single_profile_homogeneity_guard(tmp_path):
    from nanolab.config.one_shot import ArtifactReference
    from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash
    from nanolab.tasks.one_shot.qualification import read_profile

    settings, profile_set = profiles(tmp_path)
    settings.purpose = "workflow-validation"
    document = profile_set.by_node["edge-1"].model_dump(by_alias=True)
    document["purpose"] = settings.purpose
    path = tmp_path / "legacy.json"
    path.write_bytes(canonical_bytes(document))
    settings.profile = ArtifactReference(
        path=path, sha256=content_hash(path.read_bytes())
    )
    with pytest.raises(ValueError, match="homogeneous"):
        read_profile(settings)
