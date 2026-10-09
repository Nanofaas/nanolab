import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from nanolab.tasks.one_shot.experiment import collect_runtime_evidence


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
