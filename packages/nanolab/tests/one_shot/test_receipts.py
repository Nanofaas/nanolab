import copy

import pytest

from nanolab.tasks.one_shot.receipts import correlate_receipts

ROW = {
    "originalId": "original",
    "terminalExecutionId": "terminal",
    "executionId": "origin",
    "function": "work",
    "executionNode": "edge",
    "origin": "edge",
    "phase": "campaign",
    "event": "result",
}
EVENT = {
    "schemaVersion": 1,
    "runId": "run",
    "nodeId": "edge",
    "executionId": "terminal",
    "attributes": {
        "event": "runtime.execution.released",
        "receiptSchemaVersion": 1,
        "incarnation": "replica",
        "dispatchAttempt": "1",
        "state": "RELEASED",
        "occupancySeconds": 0.25,
        "responseStatus": "success",
        "handlerStarted": True,
    },
}


def test_receipts_survive_replica_removal_and_deduplicate_delivery():
    result = correlate_receipts(
        [EVENT, EVENT], [ROW], run_id="run", nodes={"edge"}, endpoints={}
    )
    assert len(result) == 1
    assert result[0]["originalId"] == "original"
    assert result[0]["executionId"] == "terminal"
    assert result[0]["destination"] == "edge"
    assert result[0]["occupancySeconds"] == 0.25


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dispatchAttempt", "2"),
        ("receiptSchemaVersion", 2),
        ("receiptSchemaVersion", True),
        ("state", "ACTIVE"),
        ("occupancySeconds", float("nan")),
        ("handlerStarted", "true"),
    ],
)
def test_invalid_receipts_never_become_physical_evidence(field, value):
    event = copy.deepcopy(EVENT)
    event["attributes"][field] = value
    with pytest.raises(ValueError, match="invalid"):
        correlate_receipts([event], [ROW], run_id="run", nodes={"edge"}, endpoints={})


def test_conflicting_receipts_are_not_silently_overwritten():
    changed = copy.deepcopy(EVENT)
    changed["attributes"]["occupancySeconds"] = 0.5
    with pytest.raises(ValueError, match="conflicting"):
        correlate_receipts(
            [EVENT, changed], [ROW], run_id="run", nodes={"edge"}, endpoints={}
        )


def test_foreign_run_is_ignored_and_wrong_destination_rejected():
    assert (
        correlate_receipts(
            [{**EVENT, "runId": "old"}],
            [ROW],
            run_id="run",
            nodes={"edge"},
            endpoints={},
        )
        == []
    )
    with pytest.raises(ValueError, match="destination"):
        correlate_receipts(
            [{**EVENT, "nodeId": "cloud"}],
            [ROW],
            run_id="run",
            nodes={"edge", "cloud"},
            endpoints={},
        )


def test_log_collection_uses_terminal_ids_without_contacting_removed_runtime(
    tmp_path, monkeypatch
):
    import json
    import time
    from contextlib import nullcontext
    from types import SimpleNamespace

    import httpx

    from nanolab.tasks.one_shot import receipts

    row = {**ROW, "scheduledAt": time.time() - 2, "statusCode": 200}
    event = {**EVENT, "runId": tmp_path.name}
    run = SimpleNamespace(
        run_dir=tmp_path,
        settings=SimpleNamespace(
            observability=SimpleNamespace(logs=SimpleNamespace(export_limit=100))
        ),
        topology=SimpleNamespace(
            platform=SimpleNamespace(receipt_endpoint="http://loki/loki/api/v1/push"),
            endpoints={"edge": "http://edge"},
        ),
    )

    def handle(request):
        assert request.url.host == "loki"
        assert (
            'attributes_event="runtime.execution.released"'
            in request.url.params["query"]
        )
        return httpx.Response(
            200,
            json={
                "data": {
                    "result": [{"values": [[str(time.time_ns()), json.dumps(event)]]}]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(receipts.httpx, "Client", lambda **_: nullcontext(client))
        receipts.collect_log_receipts(
            run,
            SimpleNamespace(resource=lambda value: value),
            [row],
            deadline=time.monotonic(),
        )
    proof = json.loads((tmp_path / "physical.jsonl").read_text())
    assert proof["originalId"] == "original"
    assert proof["executionId"] == "terminal"
    assert json.loads((tmp_path / "receipt-collection.jsonl").read_text())["complete"]


def test_collection_waits_for_release_after_http_timeout(tmp_path, monkeypatch):
    import json
    import time
    from contextlib import nullcontext
    from types import SimpleNamespace

    import httpx

    from nanolab.tasks.one_shot import receipts

    row = {**ROW, "scheduledAt": time.time() - 2, "statusCode": 504}
    event = {
        **EVENT,
        "runId": tmp_path.name,
        "attributes": {**EVENT["attributes"], "responseStatus": "timeout"},
    }
    run = SimpleNamespace(
        run_dir=tmp_path,
        settings=SimpleNamespace(
            observability=SimpleNamespace(logs=SimpleNamespace(export_limit=100))
        ),
        topology=SimpleNamespace(
            platform=SimpleNamespace(receipt_endpoint="http://loki/loki/api/v1/push"),
            endpoints={"edge": "http://edge"},
        ),
    )
    calls = []

    def handle(request):
        calls.append(request)
        values = [] if len(calls) == 1 else [[str(time.time_ns()), json.dumps(event)]]
        return httpx.Response(200, json={"data": {"result": [{"values": values}]}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(receipts.httpx, "Client", lambda **_: nullcontext(client))
        monkeypatch.setattr(receipts.time, "sleep", lambda _: None)
        receipts.collect_log_receipts(
            run,
            SimpleNamespace(resource=lambda value: value),
            [row],
            deadline=time.monotonic() + 2,
        )
    assert len(calls) == 2
    assert (
        json.loads((tmp_path / "physical.jsonl").read_text())["responseStatus"]
        == "timeout"
    )
