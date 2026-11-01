"""Warm-up computes each output oracle once and warms nodes concurrently."""

from concurrent.futures import Future
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from nanolab.tasks.one_shot import observation

REAL_CLIENT = httpx.Client


class InlineExecutor:
    """Stand in for the spawn pool: a patched oracle cannot cross processes."""

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def submit(self, fn, *args, **kwargs):
        future = Future()
        future.set_result(fn(*args, **kwargs))
        return future

    def shutdown(self, **_kwargs):
        pass


def warm_context(tmp_path, vectors):
    function = SimpleNamespace(input={"seed": 7}, memory_mib=64, cpu=0.1)
    return SimpleNamespace(
        run_dir=tmp_path,
        settings=None,
        topology=SimpleNamespace(
            function_aliases={},
            endpoints={node: node for node in vectors},
            resources=SimpleNamespace(nodes={node: node for node in vectors}),
            node_function_settings={
                node: dict.fromkeys(vector, function)
                for node, vector in vectors.items()
            },
        ),
    )


def patch_warmup(monkeypatch, vectors, measure):
    oracle_calls = []
    lock = Lock()

    def checksum(payload, *, function):
        with lock:
            oracle_calls.append(function)
        return 42

    def respond(request):
        if request.url.path == "/runtime/status":
            return httpx.Response(
                200, json={"physicalReleaseProof": True, "maxConcurrentHandlers": 1}
            )
        return httpx.Response(200, json={})

    monkeypatch.setattr(observation, "ProcessPoolExecutor", InlineExecutor)
    monkeypatch.setattr(observation, "workload_checksum", checksum)
    monkeypatch.setattr(
        observation, "comparison_replica_vectors", lambda *_args: vectors
    )
    monkeypatch.setattr(
        observation.httpx,
        "Client",
        lambda **_kwargs: REAL_CLIENT(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(
        observation,
        "runtime_endpoint",
        lambda node, name, **kw: (f"http://{node}-{name}-{kw['replica']}", {}),
    )
    monkeypatch.setattr(observation, "measure_invocation", measure)
    monkeypatch.setattr(observation, "collect_container_cgroup", lambda *_: {})
    monkeypatch.setattr(observation, "validate_strict_cgroup", lambda *_a, **_k: None)
    monkeypatch.setattr(
        observation,
        "verify_queue_admission",
        lambda *_args: {"function": "work"},
    )
    monkeypatch.setattr(observation, "event", lambda *_args, **_kwargs: None)
    return oracle_calls


def test_oracle_runs_once_per_function_and_nodes_warm_concurrently(
    tmp_path, monkeypatch
):
    vectors = {"edge-1": {"work": 1, "sort": 2}, "cloud": {"work": 2, "sort": 1}}
    together = Barrier(2, timeout=10)
    seen = []

    def measure(_http, url, **kwargs):
        # The first invocation of each node meets the other's: sequential
        # warm-up would time out here.
        if url.endswith("-work-1"):
            together.wait()
        seen.append((kwargs["execution_id"], kwargs["expected_output"]))
        return {"state": "complete"}

    oracle_calls = patch_warmup(monkeypatch, vectors, measure)
    observation.WarmInstances(warm_context(tmp_path, vectors)).run(
        cast(Any, SimpleNamespace(resource=lambda key: f"http://{key}"))
    )
    assert sorted(oracle_calls) == ["sort", "work"]
    assert sorted(seen) == sorted(
        (f"bootstrap:{node}:{name}:{replica}", 42)
        for node, vector in vectors.items()
        for name, count in vector.items()
        for replica in range(1, count + 1)
    )
    assert sorted(path.name for path in tmp_path.glob("*-warm.json")) == sorted(
        f"{node}-{name}-r{replica}-warm.json"
        for node, vector in vectors.items()
        for name, count in vector.items()
        for replica in range(1, count + 1)
    )


def test_one_node_failure_stops_the_others_and_keeps_its_error(tmp_path, monkeypatch):
    vectors = {"edge-1": {"work": 1}, "cloud": {"work": 3}}
    failed = Barrier(2, timeout=10)
    stops = []
    cloud = []

    class RecordedEvent(Event):
        def __init__(self):
            super().__init__()
            stops.append(self)

    def measure(_http, url, **_kwargs):
        if url.startswith("http://edge-1"):
            failed.wait()
            raise RuntimeError("edge-1 replica refused")
        cloud.append(url)
        failed.wait()
        # Finish this replica only once the failure has stopped the warm-up.
        assert stops[0].wait(timeout=10)
        return {"state": "complete"}

    patch_warmup(monkeypatch, vectors, measure)
    monkeypatch.setattr(observation, "Event", RecordedEvent)
    with pytest.raises(RuntimeError, match="edge-1 replica refused"):
        observation.WarmInstances(warm_context(tmp_path, vectors)).run(
            cast(Any, SimpleNamespace(resource=lambda key: f"http://{key}"))
        )
    assert cloud == ["http://cloud-work-1"]
    assert not list(tmp_path.glob("edge-1-*"))
