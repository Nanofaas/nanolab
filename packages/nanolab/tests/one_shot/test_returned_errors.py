import json

from nanolab.one_shot.models import CampaignManifest
from nanolab.one_shot.report import render_report
from nanolab.one_shot.returned_errors import classify_response, summarize_responses


def manifest():
    return CampaignManifest(
        provider="multipass",
        purpose="workflow-validation",
        environment_fingerprint="fp",
        source_commit="a" * 40,
        profile_sha256="b" * 64,
        qualification_sha256="c" * 64,
        trace_sha256="d" * 64,
        seed=7,
        nodes=["edge-0", "edge-1", "cloud"],
        functions=["work"],
        period_seconds=20,
        lead_seconds=2,
        flow_quantum=1,
        modes=["baseline", "oracle", "ewma"],
        mode="oracle",
        parameters={},
    )


def test_explicit_refusals_and_ambiguous_http_errors_are_distinct():
    assert classify_response({"statusCode": 429}) == "reject"
    assert (
        classify_response({"statusCode": 503, "responseBody": "unavailable"})
        == "httpError"
    )
    assert (
        classify_response(
            {
                "statusCode": 200,
                "responseStatus": "ERROR",
                "responseBody": json.dumps(
                    {
                        "error": {
                            "code": "EXTERNAL_ERROR",
                            "message": "Too many concurrent invocations",
                        }
                    }
                ),
            }
        )
        == "reject"
    )
    assert (
        classify_response(
            {
                "statusCode": 502,
                "offloadedTarget": "edge-2",
                "responseBody": json.dumps(
                    {
                        "error": "OFFLOAD_FAILED",
                        "message": "remote edge-2 returned 429: busy",
                    }
                ),
            }
        )
        == "reject"
    )
    assert (
        classify_response(
            {
                "statusCode": 502,
                "responseBody": json.dumps(
                    {
                        "error": "OFFLOAD_FAILED",
                        "message": "remote somewhere returned 429",
                    }
                ),
            }
        )
        == "httpError"
    )
    assert (
        classify_response({"statusCode": 200, "responseStatus": "ERROR"})
        == "applicationError"
    )
    assert classify_response({"statusCode": 408}) == "timeoutResponse"
    assert classify_response({"statusCode": 504}) == "timeoutResponse"
    assert classify_response({"statusCode": 0}) == "unresolved"
    assert (
        classify_response({"statusCode": 200, "responseStatus": "SUCCESS"})
        == "successResponse"
    )
    assert classify_response({"statusCode": 200}) == "invalidResponse"


def test_original_denominators_missing_results_duplicates_and_details():
    planned = [
        {
            "originalId": str(i),
            "phase": "campaign",
            "origin": "edge-1",
            "function": "work",
            "epoch": 0,
        }
        for i in range(5)
    ]
    events = [dict(planned[i], event="emitted") for i in range(4)]
    events += [
        dict(
            planned[i],
            event="result",
            statusCode=status,
            responseBody="busy" if status == 429 else "",
        )
        for i, status in enumerate((429, 503, 0))
    ]
    events.append(events[4].copy())
    data = summarize_responses(planned, events, mode="ewma")
    total = data["phases"]["campaign"]["total"]
    assert total["planned"] == 5 and total["emitted"] == 4
    assert total["reject"] == 1 and total["httpError"] == 1
    assert total["missingResult"] == 1 and total["notEmitted"] == 1
    assert total["unresolved"] == 1 and total["duplicateResultEvents"] == 1
    assert total["returnedErrors"] == 2
    assert total["returnedErrorsPercentOfPlanned"] == 40
    assert total["returnedErrorsPercentOfEmitted"] == 50
    assert data["phases"]["campaign"]["byCell"][0]["reject"] == 1
    assert data["errorDetails"][0]["responseBody"] == "busy"
    assert data["mode"] == "ewma"
    assert data["phases"]["warmup"]["total"]["returnedErrorsPercentOfPlanned"] is None


def test_failed_report_still_counts_raw_errors_and_keeps_raw_files(tmp_path):
    planned = [
        {
            "originalId": "a",
            "phase": "campaign",
            "origin": "edge-0",
            "function": "work",
            "epoch": 0,
        }
    ]
    (tmp_path / "schedule.json").write_text(json.dumps(planned))
    raw = "\n".join(
        json.dumps({"msg": json.dumps(dict(planned[0], event=event, **fields))})
        for event, fields in [
            ("emitted", {}),
            ("result", {"statusCode": 429, "responseBody": "busy"}),
        ]
    )
    (tmp_path / "generator.jsonl").write_text(raw)
    report = render_report(manifest(), None, tmp_path / "report.html")
    data = json.loads(report.with_suffix(".json").read_text())
    assert data["returnedResponses"]["phases"]["campaign"]["total"]["reject"] == 1
    assert not data["conforming"]
    assert (tmp_path / "generator.jsonl").read_text() == raw
    assert "Returned errors" in report.read_text()


def test_conflicting_results_are_unresolved_and_unknown_originals_are_visible():
    planned = [
        {
            "originalId": "a",
            "phase": "warmup",
            "origin": "cloud",
            "function": "work",
            "epoch": -1,
        }
    ]
    events = [
        dict(planned[0], event="emitted"),
        dict(planned[0], event="result", statusCode=429),
        dict(planned[0], event="result", statusCode=200, responseStatus="SUCCESS"),
        {"originalId": "unknown", "event": "result", "statusCode": 503},
    ]
    summary = summarize_responses(planned, events, mode="baseline")
    assert summary["phases"]["warmup"]["total"]["unresolved"] == 1
    assert summary["phases"]["warmup"]["total"]["returnedErrors"] == 0
    assert summary["phases"]["warmup"]["byNode"]["cloud"]["emitted"] == 1
    assert summary["unexpectedOriginals"] == 1


def test_actual_generator_lowercase_status_preserves_success_and_proxy_reject():
    assert (
        classify_response({"statusCode": 200, "responseStatus": "success"})
        == "successResponse"
    )
    assert (
        classify_response(
            {
                "statusCode": 200,
                "responseStatus": "error",
                "responseBody": json.dumps(
                    {
                        "error": {
                            "code": "EXTERNAL_ERROR",
                            "message": "Too many concurrent invocations",
                        }
                    }
                ),
            }
        )
        == "reject"
    )
    assert (
        classify_response(
            {
                "statusCode": 200,
                "responseStatus": "error",
                "responseBody": json.dumps(
                    {"error": {"code": "HANDLER_ERROR", "message": "failed"}}
                ),
            }
        )
        == "applicationError"
    )


def test_rust_admission_refusal_wrapped_by_control_plane_is_terminal_reject():
    from nanolab.tasks.one_shot.experiment import classify_result

    for code in ("RUNTIME_HANDLER_SATURATED", "RUNTIME_CALLBACK_SATURATED"):
        row = {
            "statusCode": 200,
            "responseStatus": "error",
            "outputJson": None,
            "responseBody": json.dumps(
                {
                    "error": {
                        "code": "EXTERNAL_ERROR",
                        "message": json.dumps(
                            {"error": {"code": code, "message": "capacity exhausted"}}
                        ),
                    }
                }
            ),
        }
        assert classify_response(row) == "reject"
        assert classify_result(row, expected="42", physical=[])["terminalError"]


def test_wrapped_external_timeout_is_reported_without_proving_execution_end():
    from nanolab.tasks.one_shot.experiment import classify_result

    row = {
        "statusCode": 200,
        "responseStatus": "error",
        "outputJson": None,
        "responseBody": json.dumps(
            {
                "error": {
                    "code": "EXTERNAL_TIMEOUT",
                    "message": "External request timed out",
                }
            }
        ),
    }
    assert classify_response(row) == "timeoutResponse"
    assert not classify_result(row, expected="42", physical=[])["terminalError"]
    row["responseBody"] = json.dumps(
        {"error": {"code": "EXTERNAL_ERROR", "message": "capacity exhausted"}}
    )
    assert classify_response(row) == "applicationError"
    assert not classify_result(row, expected="42", physical=[])["terminalError"]
