import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from nanolab.tasks.one_shot.experiment import collect_runtime_evidence


@pytest.mark.parametrize(
    "failure", [OSError(9, "Bad file descriptor"), httpx.ReadError("closed socket")]
)
@pytest.mark.parametrize("persistent", [False, True])
def test_final_proof_collection_retries_transport_read_once(
    tmp_path, monkeypatch, failure, persistent
):
    """A transient final GET must not lose an otherwise verifiable completion."""
    run = SimpleNamespace(
        run_dir=tmp_path,
        topology=SimpleNamespace(
            resources=SimpleNamespace(nodes={"cloud": SimpleNamespace()}),
            function_settings={"work": SimpleNamespace(max_replicas=1)},
            endpoints={"cloud": "endpoint"},
        ),
        sdk_request_targets={"id": ("cloud", "work", "http://sdk/invoke")},
    )
    row = {
        "originalId": "id",
        "event": "result",
        "phase": "campaign",
        "executionId": "id",
        "terminalExecutionId": "id",
        "function": "work",
        "sdk": True,
        "url": "http://sdk/invoke",
    }
    reads = []

    def handler(request):
        assert request.method == "GET"
        if request.url.path == "/runtime/executions/id":
            reads.append(str(request.url))
            if persistent or len(reads) == 1:
                raise failure
            return httpx.Response(
                200,
                json={
                    "executionId": "id",
                    "incarnation": "r",
                    "dispatchAttempt": "1",
                    "state": "RELEASED",
                    "handlerStarted": True,
                },
            )
        if request.url.path == "/runtime/status":
            return httpx.Response(200, json={"physicalReleaseProof": True})
        return httpx.Response(200, text="nanofaas_runtime_active_handlers 0\n")

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.runtime_endpoint",
        lambda *_args, **_kwargs: ("http://sdk", {}),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.httpx.Client",
            lambda **_: nullcontext(http),
        )
        collect_runtime_evidence(
            run,
            cast(Any, SimpleNamespace(resource=lambda _: "http://gateway:8080")),
            [row],
        )
    assert reads == ["http://sdk/runtime/executions/id"] * 2
    assert (tmp_path / "physical.jsonl").exists() is (not persistent)
    errors = [
        json.loads(line)
        for line in (tmp_path / "collection-errors.jsonl").read_text().splitlines()
    ]
    assert any(str(failure) in error["error"] for error in errors)
    if not persistent:
        proof = json.loads((tmp_path / "physical.jsonl").read_text())
        assert proof["originalId"] == "id" and proof["state"] == "RELEASED"


@pytest.mark.parametrize("identity", ["remote", "wrong"])
def test_live_collection_uses_remote_id_and_preserves_invalid_identity_diagnostic(
    tmp_path, monkeypatch, identity
):
    node = SimpleNamespace(config=SimpleNamespace(memory_capacity_mib=128))
    topology = SimpleNamespace(
        resources=SimpleNamespace(nodes={"cloud": node}),
        function_settings={"work": SimpleNamespace(max_replicas=1)},
        endpoints={"cloud": "cloud-endpoint"},
    )
    run = SimpleNamespace(topology=topology, run_dir=tmp_path)
    row = {
        "event": "result",
        "phase": "campaign",
        "originalId": "original",
        "executionId": "origin",
        "terminalExecutionId": "remote",
        "function": "work",
    }
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/runtime/status":
            return httpx.Response(
                200,
                json={
                    "maxConcurrentHandlers": 1,
                    "activeHandlers": 0,
                    "physicalReleaseProof": True,
                },
            )
        if request.url.path.endswith("remote"):
            return httpx.Response(
                200,
                json={
                    "executionId": identity,
                    "incarnation": "r",
                    "dispatchAttempt": "1",
                    "state": "RELEASED",
                },
            )
        return httpx.Response(200, text="nanofaas_runtime_active_handlers 0\n")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.httpx.Client",
            lambda **_: nullcontext(http),
        )
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.runtime_endpoint",
            lambda *_args, **_kwargs: ("http://sdk", {}),
        )
        inputs = cast(Any, SimpleNamespace(resource=lambda _: "http://gateway:8080"))
        collect_runtime_evidence(run, inputs, [row])
    assert "http://sdk/runtime/executions/remote" in requests
    assert (tmp_path / "metrics.jsonl").exists()
    if identity == "remote":
        proof = json.loads((tmp_path / "physical.jsonl").read_text())
        assert proof["originalId"] == "original" and proof["executionId"] == "remote"
    else:
        assert not (tmp_path / "physical.jsonl").exists()
        assert "identity" in (tmp_path / "collection-errors.jsonl").read_text()


@pytest.mark.parametrize("fixed", [False, True])
def test_live_collector_observes_replica_beyond_global_default(
    tmp_path, monkeypatch, fixed
):
    """Cloud replica seven must not vanish because the global default is two."""
    node = SimpleNamespace(config=SimpleNamespace(memory_capacity_mib=384))
    topology = SimpleNamespace(
        resources=SimpleNamespace(nodes={"cloud": node}),
        function_settings={"work": SimpleNamespace(max_replicas=1)},
        node_function_settings={"cloud": {"work": SimpleNamespace(max_replicas=3)}},
        endpoints={"cloud": "cloud-endpoint"},
    )
    run = SimpleNamespace(topology=topology, run_dir=tmp_path)
    if fixed:
        run.runtime_replica_counts = {"cloud": {"work": 1}}
    row = {
        "event": "result",
        "phase": "campaign",
        "originalId": "original",
        "executionId": "origin",
        "terminalExecutionId": "remote",
        "function": "work",
    }

    def handler(request):
        if request.url.path == "/runtime/status":
            return httpx.Response(
                200,
                json={
                    "maxConcurrentHandlers": 1,
                    "activeHandlers": 0,
                    "physicalReleaseProof": True,
                },
            )
        if request.url.path.endswith("remote"):
            if request.url.host != "sdk-3":
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "executionId": "remote",
                    "incarnation": "r3",
                    "dispatchAttempt": "1",
                    "state": "RELEASED",
                },
            )
        return httpx.Response(200, text="nanofaas_runtime_active_handlers 0\n")

    probed = []

    def endpoint(*args, **kwargs):
        probed.append(kwargs["replica"])
        return "http://sdk-" + str(kwargs["replica"]), {}

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.httpx.Client",
            lambda **_: nullcontext(http),
        )
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.runtime_endpoint",
            endpoint,
        )
        inputs = cast(Any, SimpleNamespace(resource=lambda _: "http://gateway:8080"))
        collect_runtime_evidence(run, inputs, [row])
    if fixed:
        assert probed == [1]
        assert not (tmp_path / "physical.jsonl").exists()
        return
    assert probed == [1, 2, 3]
    assert (tmp_path / "physical.jsonl").is_file()
    proof = json.loads((tmp_path / "physical.jsonl").read_text())
    assert proof["incarnation"] == "r3"
    assert proof["destination"] == "cloud"


def test_multifunction_collector_uses_one_verified_inventory_per_node(
    tmp_path, monkeypatch
):
    node = SimpleNamespace()
    names = [f"f{i}" for i in range(5)]
    topology = SimpleNamespace(
        resources=SimpleNamespace(nodes={"cloud": node}),
        function_settings={name: SimpleNamespace(max_replicas=35) for name in names},
        endpoints={"cloud": "endpoint"},
    )
    run = SimpleNamespace(
        topology=topology,
        run_dir=tmp_path,
        settings=SimpleNamespace(strict_memory=True, functions=dict.fromkeys(names)),
    )
    calls = []

    def inventory(*args, **kwargs):
        calls.append(kwargs["functions"])
        return [
            (name, replica, f"http://sdk-{name}-{replica}", {})
            for name in names
            for replica in range(1, 8)
        ]

    def handler(request):
        if request.url.path == "/runtime/status":
            return httpx.Response(
                200,
                json={
                    "maxConcurrentHandlers": 1,
                    "activeHandlers": 0,
                    "physicalReleaseProof": True,
                },
            )
        return httpx.Response(200, text="nanofaas_runtime_active_handlers 0\n")

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.runtime_inventory", inventory, raising=False
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.runtime_endpoint",
        lambda *args, **kwargs: pytest.fail(
            "per-replica Multipass discovery is forbidden "
            "for strict multi-function collection"
        ),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.httpx.Client",
            lambda **_: nullcontext(http),
        )
        collect_runtime_evidence(
            run,
            cast(Any, SimpleNamespace(resource=lambda _: "http://gateway:8080")),
            [],
        )
    assert calls == [names]
    rows = [
        json.loads(line)
        for line in (tmp_path / "runtime-inventory.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 35 and {row["function"] for row in rows} == set(names)


def test_batched_inventory_verifies_labels_identity_and_replica_uniqueness(monkeypatch):
    from sonata_engine import TaskInputs

    from nanolab.tasks.one_shot.preflight import runtime_inventory
    from tests.tasks.test_managed_container_inspection import ContainerEngine

    engine = ContainerEngine("ignored")
    engine.object["Config"]["Labels"]["io.nanofaas.function"] = "owned/f0"
    engine.object["NetworkSettings"] = {"Ports": {"8080/tcp": [{"HostPort": "1234"}]}}
    node = cast(Any, SimpleNamespace(request=SimpleNamespace(name="owned"), vm="vm"))
    monkeypatch.setattr(
        "nanolab.one_shot.infrastructure.NodeExecutor", lambda _: engine
    )
    inputs = cast(
        TaskInputs, SimpleNamespace(resource=lambda _: SimpleNamespace(host="10.1.2.3"))
    )
    inventory = runtime_inventory(node, functions=["f0", "f1"], inputs=inputs)
    assert [(name, replica, url) for name, replica, url, _ in inventory] == [
        ("f0", 1, "http://10.1.2.3:1234")
    ]
    assert len(engine.seen) == 2
    engine.object["NetworkSettings"]["Ports"]["8080/tcp"].append(
        {"HostPort": "1234", "HostIp": "::"}
    )
    assert len(runtime_inventory(node, functions=["f0", "f1"], inputs=inputs)) == 1
    engine.object["NetworkSettings"]["Ports"]["8080/tcp"][1]["HostPort"] = "9999"
    with pytest.raises(ValueError, match="port"):
        runtime_inventory(node, functions=["f0", "f1"], inputs=inputs)
    engine.object["NetworkSettings"]["Ports"]["8080/tcp"].pop()
    engine.object["Config"]["Labels"]["io.nanofaas.replica"] = "0"
    with pytest.raises(ValueError, match="replica"):
        runtime_inventory(node, functions=["f0", "f1"], inputs=inputs)
    engine.object["Config"]["Labels"]["io.nanofaas.replica"] = "1"
    engine.object["Id"] = "b" * 64
    with pytest.raises(ValueError, match="identity"):
        runtime_inventory(node, functions=["f0", "f1"], inputs=inputs)


@pytest.mark.parametrize("wrong_target", [False, True])
def test_direct_sdk_proofs_use_frozen_request_target_and_still_require_release(
    tmp_path, monkeypatch, wrong_target
):
    calls = []
    run = SimpleNamespace(
        run_dir=tmp_path,
        topology=SimpleNamespace(
            resources=SimpleNamespace(nodes={"cloud": SimpleNamespace()}),
            function_settings={"work": SimpleNamespace(max_replicas=3)},
            endpoints={"cloud": "endpoint"},
        ),
        sdk_request_targets={"id": ("cloud", "work", "http://sdk-2/invoke")},
    )
    row = {
        "originalId": "id",
        "event": "result",
        "phase": "campaign",
        "executionId": "id",
        "terminalExecutionId": "id",
        "function": "work",
        "sdk": True,
        "url": "http://sdk-3/invoke" if wrong_target else "http://sdk-2/invoke",
    }

    def handler(request):
        if "/runtime/executions/" in request.url.path:
            calls.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "executionId": "id",
                    "incarnation": "r2",
                    "dispatchAttempt": "1",
                    "state": "RELEASED",
                    "handlerStarted": True,
                },
            )
        if request.url.path == "/runtime/status":
            return httpx.Response(
                200,
                json={
                    "maxConcurrentHandlers": 1,
                    "activeHandlers": 0,
                    "physicalReleaseProof": True,
                },
            )
        return httpx.Response(200, text="nanofaas_runtime_active_handlers 0\n")

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.runtime_endpoint",
        lambda *_args, **kwargs: (f"http://sdk-{kwargs['replica']}", {}),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.httpx.Client",
            lambda **_: nullcontext(http),
        )
        collect_runtime_evidence(
            run,
            cast(Any, SimpleNamespace(resource=lambda _: "http://gateway:8080")),
            [row],
        )
    assert calls == ([] if wrong_target else ["http://sdk-2/runtime/executions/id"])
    assert (tmp_path / "physical.jsonl").exists() is (not wrong_target)
