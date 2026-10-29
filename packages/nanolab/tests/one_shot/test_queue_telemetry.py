import json
from threading import Event

import httpx

from nanolab.tasks.one_shot.queue_telemetry import (
    QueueTelemetryCollector,
    queue_samples,
)


def test_queue_samples_keep_function_labels_and_short_rejections():
    rows = queue_samples("""# TYPE sync_queue_depth gauge
sync_queue_depth{function=""} 3
sync_queue_depth{function="a"} 3
sync_queue_depth{function="b"} 0
sync_queue_rejected_total{function="b",reason="depth"} 2
process_start_time_seconds 1234
unrelated_metric 99
""")
    assert rows == [
        {"name": "sync_queue_depth", "labels": {"function": ""}, "value": 3.0},
        {"name": "sync_queue_depth", "labels": {"function": "a"}, "value": 3.0},
        {"name": "sync_queue_depth", "labels": {"function": "b"}, "value": 0.0},
        {
            "name": "sync_queue_rejected_total",
            "labels": {"function": "b", "reason": "depth"},
            "value": 2.0,
        },
        {"name": "process_start_time_seconds", "labels": {}, "value": 1234.0},
    ]


def test_sampler_keeps_errors_and_restart_without_inventing_zero(tmp_path):
    done = Event()
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 2:
            return httpx.Response(503)
        if len(requests) == 3:
            done.set()
        start = 10 if len(requests) == 1 else 20
        return httpx.Response(
            200,
            text=(
                f"process_start_time_seconds {start}\n"
                'sync_queue_depth{function="a"} 1\n'
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        collector = QueueTelemetryCollector(
            tmp_path, "run", {"edge": "http://edge:8080"}, http=http
        )
        try:
            assert done.wait(4)
        finally:
            collector.close()
    rows = [
        json.loads(line)
        for line in (tmp_path / "queue-samples-edge.jsonl").read_text().splitlines()
    ]
    assert len(rows) >= 3
    assert rows[0]["samples"][0]["value"] == 10
    assert rows[1]["measurementComplete"] is False
    assert "samples" not in rows[1]
    assert rows[2]["samples"][0]["value"] == 20
    assert all(row["node"] == "edge" and row["runId"] == "run" for row in rows)
    assert (
        rows[1]["scheduledMonotonicSeconds"] - rows[0]["scheduledMonotonicSeconds"] == 1
    )
    assert requests[0].url == "http://edge:9090/actuator/prometheus"


def test_queue_log_export_pages_without_losing_timestamp_boundary(tmp_path):
    from nanolab.tasks.one_shot.logging import export_queue_events

    def event(n):
        return json.dumps({"attributes": {"event": "QUEUED", "executionId": str(n)}})

    pages = [
        [["10", event(1)], ["20", event(2)]],
        [["20", event(2)], ["30", event(3)]],
        [["30", event(3)]],
    ]
    starts = []

    def respond(request):
        starts.append(request.url.params["start"])
        return httpx.Response(
            200, json={"data": {"result": [{"values": pages.pop(0)}]}}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        count = export_queue_events(
            http,
            "http://logs/query_range",
            "query",
            1,
            40,
            2,
            tmp_path / "queue-events.jsonl",
        )
    rows = [
        json.loads(line)
        for line in (tmp_path / "queue-events.jsonl").read_text().splitlines()
    ]
    assert count == 3
    assert [row["attributes"]["executionId"] for row in rows] == ["1", "2", "3"]
    assert starts == ["1", "20", "30"]


def test_queue_waits_use_same_process_and_keep_missing_events_unknown():
    from nanolab.tasks.one_shot.queue_telemetry import queue_timings

    def event(process, identity, stage, nanos):
        return {
            "attributes": {
                "processId": process,
                "executionId": identity,
                "attempt": 1,
                "originalId": identity,
                "function": "f",
                "event": stage,
                "monotonicNanos": nanos,
            }
        }

    rows = [
        event("p1", "a", "QUEUED", 100),
        event("p1", "a", "DISPATCH", 200),
        event("p1", "a", "RETURNED", 300),
        event("p1", "a", "DISPATCH", 500),
        event("p1", "b", "QUEUED", 100),
        event("p1", "b", "EXPIRED", 600),
        event("p2", "a", "DISPATCH", 900),
    ]
    measured = queue_timings(list(reversed(rows)))
    by_key = {(row["processId"], row["executionId"]): row for row in measured}
    assert by_key["p1", "a"]["queueWaitSeconds"] == 300 / 1e9
    assert by_key["p1", "b"]["queueWaitSeconds"] == 500 / 1e9
    assert by_key["p1", "b"]["queueOutcome"] == "expired"
    assert by_key["p2", "a"]["queueWaitSeconds"] is None
    assert by_key["p2", "a"]["measurementComplete"] is False
