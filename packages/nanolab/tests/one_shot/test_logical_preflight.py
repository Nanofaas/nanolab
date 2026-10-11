"""Logical readiness is independent of UTC; raw timing evidence stays explicit."""

from types import SimpleNamespace

import httpx
import pytest

from nanolab.tasks.one_shot.preflight import measure_http_clock, validate_node
from tests.one_shot.test_preflight import evidence


@pytest.mark.parametrize("offset", [30.0, None])
def test_logical_preflight_accepts_skew_or_missing_clock_without_weakening_memory(
    offset,
):
    row = evidence(
        clock_offset_seconds=offset,
        clock_uncertainty_seconds=None if offset is None else 0.5,
        rtt_seconds=None if offset is None else 1.0,
        clock_status="MISSING" if offset is None else "MEASURED",
    )
    validate_node(row, max_clock_skew_seconds=0.1, protocol_version=2)
    with pytest.raises(ValueError, match="clock"):
        validate_node(row, max_clock_skew_seconds=0.1)
    with pytest.raises(ValueError, match="memory"):
        validate_node(
            row.model_copy(update={"memory_capacity_mib": 5000}),
            max_clock_skew_seconds=0.1,
            protocol_version=2,
        )


def test_logical_probe_keeps_failed_attempts_and_monotonic_brackets(monkeypatch):
    wall = iter([100.0, 101.0, 200.0, 230.004, 300.0, 300.002])
    mono = iter([10.0, 11.0, 12.0, 12.004, 13.0, 13.002])
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.time",
        SimpleNamespace(time=lambda: next(wall), monotonic=lambda: next(mono)),
    )
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadTimeout("missing", request=request)
        return httpx.Response(
            200,
            json={
                "timestamp": 215.002 if count == 2 else 300.001,
                "monotonicSeconds": 50.0 if count == 2 else 51.0,
                "bootId": "guest-boot",
            },
        )

    attempts = []
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        offset, uncertainty, _ = measure_http_clock(
            "http://node",
            http=http,
            repetitions=3,
            record_probe=attempts.append,
            tolerate_failures=True,
        )
    assert len(attempts) == 3
    assert attempts[0]["errorType"] == "ReadTimeout"
    assert attempts[1]["rttSeconds"] == pytest.approx(0.004)
    assert attempts[1]["hostUtcJumpSeconds"] == pytest.approx(30)
    assert attempts[2]["beforeMonotonicSeconds"] == 13.0
    assert attempts[2]["afterMonotonicSeconds"] == 13.002
    assert attempts[2]["remoteMonotonicSeconds"] == 51.0
    assert attempts[2]["remoteBootId"] == "guest-boot"
    assert offset == pytest.approx(0)
    assert uncertainty == pytest.approx(0.001)


def test_logical_probe_does_not_hide_all_missing_attempts_or_storage_failure():
    attempts = []

    def fail(request):
        raise httpx.ConnectError("missing", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        with pytest.raises(httpx.ConnectError):
            measure_http_clock(
                "http://node",
                http=http,
                repetitions=3,
                record_probe=attempts.append,
                tolerate_failures=True,
            )
        assert len(attempts) == 3

        def storage_failure(_sample):
            raise OSError("evidence disk unavailable")

        with pytest.raises(OSError, match="disk"):
            measure_http_clock(
                "http://node",
                http=http,
                repetitions=3,
                record_probe=storage_failure,
                tolerate_failures=True,
            )
