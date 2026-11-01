import csv
import json

import pytest

from nanolab.assets.diagnostics.idle_reactivation_probe import load_inputs
from nanolab.tasks.offload_experiment_report import analyze_experiment


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def inventory():
    pods = []
    for container, name, labels in (
        ("control-plane", "cp", {"app": "nanofaas-control-plane"}),
        ("function", "fn", {"function": "word-stats-java"}),
    ):
        pods.append(
            {
                "kind": "Pod",
                "metadata": {"uid": name, "name": name, "labels": labels},
                "spec": {"containers": [{"name": container}]},
                "status": {
                    "containerStatuses": [
                        {
                            "name": container,
                            "imageID": "sha256:" + name,
                            "restartCount": 0,
                            "ready": True,
                        }
                    ]
                },
            }
        )
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "fn-word-stats-java"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"function": "word-stats-java"}},
        },
    }
    return {"items": [deployment, *pods]}


@pytest.fixture
def run(tmp_path):
    write(
        tmp_path,
        "protocol.json",
        {
            "function": "word-stats-java",
            "edge_url": "http://edge",
            "cloud_url": "http://cloud",
            "source_fingerprint": "abc",
            "scenario": {
                "offloadExperiment": {
                    "mode": "pressure",
                    "rate": 4,
                    "durationSeconds": 1,
                    "warmupSeconds": 1,
                    "vus": 512,
                }
            },
        },
    )
    cases = [{"name": f"case-{i}", "input": {"text": str(i)}} for i in range(4)]
    write(
        tmp_path,
        "inputs/performance-medium.json",
        {"profile": "medium", "cases": cases},
    )
    write(
        tmp_path,
        "inputs/runtime-contract.json",
        {
            "contract": "word-stats-runtime-v1",
            "cases": [
                dict(c, name="benchmark " + c["name"], expected={"count": i})
                for i, c in enumerate(cases)
            ],
        },
    )
    fixtures = load_inputs(
        tmp_path / "inputs/performance-medium.json",
        tmp_path / "inputs/runtime-contract.json",
    )
    records = []
    for phase, target, count in (
        ("qualification", "edge", 4),
        ("qualification", "cloud", 4),
        ("warmup", "edge", 20),
        ("measurement", "edge", 4),
    ):
        for i in range(count):
            fixture = fixtures[i % 4]
            records.append(
                {
                    "t": "b4",
                    "phase": phase,
                    "target": target,
                    "iteration": i,
                    "case_name": fixture["name"],
                    "status": 200,
                    "body": json.dumps(
                        {"status": "success", "output": fixture["expected"]}
                    ),
                    "offloaded": None,
                    "duration_ms": 10 + i,
                    "http_duration_ms": 5 + i,
                    "ts": 100000 + i,
                    "error_code": 0,
                }
            )
    set_records(tmp_path, records)
    write(tmp_path, "k6-summary.json", {"metrics": {"iterations": {"count": 24}}})
    write(tmp_path, "execution.json", {"k6_passed": True, "errors": []})
    for node in ("stack", "cloud"):
        for phase, timestamp, success in (("before", 1, 0), ("after", 2, 32)):
            metrics = f"process_uptime_seconds {timestamp}\n"
            for name, value in (
                ("function_success_total", success),
                ("function_retry_total", 0),
                ("nanofaas_offload_total", 0),
                ("nanofaas_offload_failure_total", 0),
            ):
                metrics += f'{name}{{function="word-stats-java"}} {value}\n'
            write(
                tmp_path,
                f"observations/{node}-{phase}.json",
                {
                    "captured_at": timestamp,
                    "metrics": metrics,
                    "inventory": inventory(),
                },
            )
    return tmp_path


def get_records(root):
    return [
        json.loads(line) for line in (root / "k6-events.jsonl").read_text().splitlines()
    ]


def set_records(root, records):
    (root / "k6-events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records)
    )


def test_valid_zero_offload_and_failure_meters(run):
    report = analyze_experiment(run)
    assert report["integrity_passed"], report["errors"]
    assert report["counts"]["measurement"] == {
        "planned": 4,
        "scheduled": 4,
        "boundary_extra": 0,
        "emitted": 4,
        "dropped": 0,
        "missing": 0,
        "drop_attribution": "inferred from reconciled complete ledger",
    }
    assert report["latency_ms"]["offloaded"]["p95"] is None
    assert report["goodput_per_second"] == 4
    assert report["diagnostics"]["stack"]["deltas"]["function_success_total"] == 32
    assert (run / "report.md").exists()
    with (run / "measured-requests.csv").open() as stream:
        assert len(list(csv.DictReader(stream))) == 4


def test_zero_success_is_valid_and_http_failures_stay_outcomes(run):
    records = get_records(run)
    for record, status in zip(records[-4:], (429, 503, 500, 0), strict=True):
        record.update(status=status, body="failure")
        if status == 0:
            record["error_code"] = 1050
    set_records(run, records)
    report = analyze_experiment(run)
    assert report["integrity_passed"], report["errors"]
    assert report["outcomes"] == {
        "success": 0,
        "refused": 2,
        "http_error": 1,
        "transport_error": 1,
        "invalid_body": 0,
    }
    assert report["goodput_per_second"] == 0
    assert report["latency_ms"]["local"]["p50"] is None


def test_drops_are_distinct_from_missing_records(run):
    set_records(run, get_records(run)[:-1])
    write(
        run,
        "k6-summary.json",
        {"metrics": {"iterations": {"count": 23}, "dropped_iterations": {"count": 1}}},
    )
    report = analyze_experiment(run)
    assert report["integrity_passed"], report["errors"]
    assert report["counts"]["total"]["dropped"] == 1
    assert report["counts"]["total"]["missing"] == 0
    assert report["counts"]["measurement"]["emitted"] == 3
    assert report["completion_probability_scheduled"] == 0.75
    assert report["completion_probability_emitted"] == 1


def test_missing_record_is_invalid_and_preserved(run):
    set_records(run, get_records(run)[:-1])
    report = analyze_experiment(run)
    assert not report["integrity_passed"]
    assert report["counts"]["total"]["missing"] == 1
    assert report["counts"]["measurement"]["missing"] is None


@pytest.mark.parametrize(
    "name",
    [
        "protocol.json",
        "k6-events.jsonl",
        "k6-summary.json",
        "execution.json",
        "observations/stack-before.json",
    ],
)
def test_missing_evidence_always_saves_a_failed_verdict(run, name):
    (run / name).unlink()
    report = analyze_experiment(run)
    assert not report["integrity_passed"]
    assert json.loads((run / "report.json").read_text()) == report


@pytest.mark.parametrize(
    "value", [[], {"scenario": []}, {"scenario": {"offloadExperiment": None}}]
)
def test_malformed_protocol_saves_verdict(run, value):
    write(run, "protocol.json", value)
    assert not analyze_experiment(run)["integrity_passed"]


def test_invalid_body_not_hidden_and_boolean_does_not_equal_number(run):
    records = get_records(run)
    records[-3]["body"] = json.dumps({"status": "success", "output": {"count": True}})
    set_records(run, records)
    report = analyze_experiment(run)
    assert not report["integrity_passed"]
    assert report["outcomes"]["invalid_body"] == 1
    assert report["outcomes"]["success"] == 3


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("duration_ms", float("nan")),
        ("iteration", 0),
        ("case_name", "wrong"),
        ("target", "cloud"),
    ],
)
def test_request_integrity(run, key, value):
    records = get_records(run)
    records[-1][key] = value
    set_records(run, records)
    assert not analyze_experiment(run)["integrity_passed"]


def test_standard_k6_console_wrapping_and_proxy_latency(run):
    set_metric(run, "stack", "after", "nanofaas_offload_total", 1)
    records = get_records(run)
    records[-1].update(offloaded="http://cloud", duration_ms=999)
    (run / "k6-events.jsonl").write_text(
        "\n".join(
            'time="2026-11-01" level=info '
            f"msg={json.dumps(json.dumps(r))} source=console"
            for r in records
        )
    )
    report = analyze_experiment(run)
    assert report["integrity_passed"], report["errors"]
    assert report["latency_ms"]["offloaded"]["p99"] == 999
    assert report["latency_ms"]["local"]["count"] == 3


def test_counter_reset_cannot_produce_delta(run):
    name = "observations/stack-after.json"
    data = json.loads((run / name).read_text())
    data["metrics"] = data["metrics"].replace(
        "process_uptime_seconds 2", "process_uptime_seconds 0"
    )
    write(run, name, data)
    report = analyze_experiment(run)
    assert not report["integrity_passed"]
    assert report["diagnostics"]["stack"]["deltas"]["function_success_total"] is None


def test_counter_decrease_is_invalid(run):
    name = "observations/stack-before.json"
    data = json.loads((run / name).read_text())
    data["metrics"] = data["metrics"].replace(
        'function_success_total{function="word-stats-java"} 0',
        'function_success_total{function="word-stats-java"} 100',
    )
    write(run, name, data)
    report = analyze_experiment(run)
    assert not report["integrity_passed"]
    assert report["diagnostics"]["stack"]["deltas"]["function_success_total"] is None


def test_lazy_offload_meters_are_unavailable_not_zero(run):
    for path in (run / "observations").glob("*.json"):
        data = json.loads(path.read_text())
        data["metrics"] = "\n".join(
            line
            for line in data["metrics"].splitlines()
            if not line.startswith("nanofaas_offload")
        )
        path.write_text(json.dumps(data))
    report = analyze_experiment(run)
    assert report["integrity_passed"], report["errors"]
    assert report["diagnostics"]["stack"]["deltas"]["nanofaas_offload_total"] is None


def test_failed_k6_is_not_ignored(run):
    write(run, "execution.json", {"k6_passed": False, "errors": ["interrupted"]})
    assert not analyze_experiment(run)["integrity_passed"]


def test_missing_required_zero_metric_is_invalid(run):
    name = "observations/cloud-before.json"
    data = json.loads((run / name).read_text())
    data["metrics"] = "\n".join(
        line
        for line in data["metrics"].splitlines()
        if not line.startswith("function_retry_total")
    )
    write(run, name, data)
    assert not analyze_experiment(run)["integrity_passed"]


def test_per_phase_drop_counters_prevent_swapping_omissions(run):
    set_records(run, get_records(run)[:-1])
    write(
        run,
        "k6-summary.json",
        {
            "metrics": {
                "iterations": {"count": 23},
                "dropped_iterations": {"count": 1},
                "dropped_iterations{scenario:warmup}": {"count": 1},
                "dropped_iterations{scenario:measurement}": {"count": 0},
            }
        },
    )
    report = analyze_experiment(run)
    assert not report["integrity_passed"]
    assert report["counts"]["measurement"]["missing"] == 1


def test_image_or_uid_change_invalidates_counter_deltas(run):
    name = "observations/cloud-after.json"
    data = json.loads((run / name).read_text())
    data["inventory"]["items"][1]["metadata"]["uid"] = "replacement"
    write(run, name, data)
    report = analyze_experiment(run)
    assert not report["integrity_passed"]
    assert report["diagnostics"]["cloud"]["deltas"]["function_success_total"] is None


def test_nonfinite_protocol_saves_failed_report(run):
    (run / "protocol.json").write_text('{"scenario": NaN}')
    assert not analyze_experiment(run)["integrity_passed"]


def test_disabled_marker_rejected(run):
    protocol = json.loads((run / "protocol.json").read_text())
    protocol["scenario"]["offloadExperiment"]["mode"] = "disabled"
    write(run, "protocol.json", protocol)
    records = get_records(run)
    records[-1]["offloaded"] = "http://cloud"
    set_records(run, records)
    assert not analyze_experiment(run)["integrity_passed"]


def test_cloud_direct_uses_cloud_ledger(run):
    protocol = json.loads((run / "protocol.json").read_text())
    protocol["scenario"]["offloadExperiment"]["mode"] = "cloud"
    write(run, "protocol.json", protocol)
    records = get_records(run)
    for record in records:
        if record["phase"] != "qualification":
            record["target"] = "cloud"
    set_records(run, records)
    assert analyze_experiment(run)["integrity_passed"]


def set_metric(root, node, phase, metric, value):
    name = f"observations/{node}-{phase}.json"
    data = json.loads((root / name).read_text())
    lines = [
        line
        for line in data["metrics"].splitlines()
        if not line.startswith(metric + "{")
    ]
    if value is not None:
        lines.append(f'{metric}{{function="word-stats-java"}} {value}')
    data["metrics"] = "\n".join(lines)
    write(root, name, data)


@pytest.mark.parametrize("node", ["stack", "cloud"])
def test_acknowledged_successes_cannot_exceed_server_delta(run, node):
    set_metric(run, node, "after", "function_success_total", 0)
    report = analyze_experiment(run)
    assert not report["integrity_passed"]
    assert any("client successes exceed" in error for error in report["errors"])


@pytest.mark.parametrize(
    ("before", "after", "valid"),
    [
        (None, None, False),
        (None, 0, False),
        (None, 1, True),
        (0, None, False),
        (0, 0, False),
        (0, 1, True),
        (4, 4, False),
        (4, 5, True),
    ],
)
def test_observed_offload_requires_final_counter_corroboration(
    run, before, after, valid
):
    records = get_records(run)
    records[-1]["offloaded"] = "http://cloud"
    set_records(run, records)
    set_metric(run, "stack", "before", "nanofaas_offload_total", before)
    set_metric(run, "stack", "after", "nanofaas_offload_total", after)
    report = analyze_experiment(run)
    assert report["integrity_passed"] is valid, report["errors"]
    assert report["latency_ms"]["offloaded"]["count"] == 1
    if before is None:
        assert (
            report["diagnostics"]["stack"]["deltas"]["nanofaas_offload_total"] is None
        )


@pytest.mark.parametrize("marker", ["true", "http://unconfigured-cloud"])
def test_non_destination_offload_marker_is_rejected(run, marker):
    records = get_records(run)
    records[-1]["offloaded"] = marker
    set_records(run, records)
    assert not analyze_experiment(run)["integrity_passed"]


def test_overall_latency_conditions_on_success_across_paths(run):
    records = get_records(run)
    records[-4].update(status=429, body="refused", duration_ms=1)
    records[-3].update(duration_ms=20)
    records[-2].update(duration_ms=30)
    records[-1].update(offloaded="http://cloud", duration_ms=999, ts=150000)
    set_records(run, records)
    set_metric(run, "stack", "after", "nanofaas_offload_total", 1)
    report = analyze_experiment(run)
    assert report["integrity_passed"], report["errors"]
    assert report["latency_ms"]["overall"] == {
        "count": 3,
        "method": "nearest rank",
        "p50": 30,
        "p95": 999,
        "p99": 999,
    }
    assert report["latency_ms"]["local"]["count"] == 2
    assert report["latency_ms"]["offloaded"]["count"] == 1
    assert report["goodput_per_second"] == 3
    assert "including drain" in report["goodput_boundary"]
    assert "not observed wall-clock throughput" in (run / "report.md").read_text()


def add_boundary_events(root, extra):
    records = get_records(root)
    template = next(r for r in records if r["phase"] == "measurement")
    for iteration in range(4, 4 + extra):
        fixture = next(
            r
            for r in records
            if r["phase"] == "measurement" and r["iteration"] == iteration % 4
        )
        records.append(
            dict(
                template,
                iteration=iteration,
                case_name=fixture["case_name"],
                body=fixture["body"],
            )
        )
    set_records(root, records)
    write(
        root,
        "k6-summary.json",
        {
            "metrics": {
                "iterations": {"count": 24 + extra},
                "dropped_iterations{scenario:warmup}": {"count": 0},
                "dropped_iterations{scenario:measurement}": {"count": 0},
            }
        },
    )


def test_one_accounted_k6_boundary_iteration_uses_actual_scheduled_denominator(run):
    add_boundary_events(run, 1)
    report = analyze_experiment(run)
    assert report["integrity_passed"], report["errors"]
    assert report["counts"]["measurement"]["planned"] == 4
    assert report["counts"]["measurement"]["scheduled"] == 5
    assert report["counts"]["measurement"]["boundary_extra"] == 1
    assert report["completion_probability_scheduled"] == 1
    assert report["goodput_per_second"] == 5


def test_boundary_tolerance_does_not_hide_missing_event(run):
    add_boundary_events(run, 1)
    set_records(run, get_records(run)[:-1])
    report = analyze_experiment(run)
    assert not report["integrity_passed"]
    assert report["counts"]["total"]["missing_ledger_records"] == 1
    assert report["completion_probability_scheduled"] is None


def test_more_than_one_boundary_iteration_is_rejected(run):
    add_boundary_events(run, 2)
    assert not analyze_experiment(run)["integrity_passed"]


def test_boundary_iteration_requires_phase_drop_evidence(run):
    add_boundary_events(run, 1)
    write(run, "k6-summary.json", {"metrics": {"iterations": {"count": 25}}})
    assert not analyze_experiment(run)["integrity_passed"]


def test_boundary_drop_is_counted_in_actual_scheduled_denominator(run):
    write(
        run,
        "k6-summary.json",
        {
            "metrics": {
                "iterations": {"count": 24},
                "dropped_iterations": {"count": 1},
                "dropped_iterations{scenario:warmup}": {"count": 0},
                "dropped_iterations{scenario:measurement}": {"count": 1},
            }
        },
    )
    report = analyze_experiment(run)
    assert report["integrity_passed"], report["errors"]
    assert report["counts"]["measurement"]["scheduled"] == 5
    assert report["counts"]["measurement"]["dropped"] == 1
    assert report["counts"]["measurement"]["missing"] == 0
    assert report["completion_probability_scheduled"] == 0.8
