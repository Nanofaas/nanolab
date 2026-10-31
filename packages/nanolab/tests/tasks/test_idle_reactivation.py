import importlib
from copy import deepcopy

import pytest


def measurements():
    return importlib.import_module("nanolab.tasks.idle_reactivation")


def observation(t, *, desired=0, pods=None):
    return {
        "time": t,
        "desired": desired,
        "ready": desired,
        "pods": pods or [],
        "control_plane": [{"uid": "cp", "restarts": 0, "image_id": "sha256:cp"}],
    }


def pod(uid="new", **changes):
    return {
        "uid": uid,
        "name": "fn-test-" + uid,
        "restarts": 0,
        "ready": True,
        "deleting": False,
        "image_id": "sha256:fn",
    } | changes


def test_zero_requires_full_window_no_terminating_pods_and_no_unknown_samples():
    m = measurements()
    window = [observation(t) for t in range(0, 61, 2)]
    assert m.qualifies_idle(window, 0, 60, "scale-to-zero")
    stale = deepcopy(window)
    stale[5]["pods"] = [pod("old", deleting=True)]
    assert not m.qualifies_idle(stale, 0, 60, "scale-to-zero")
    missing = deepcopy(window)
    missing[10] = {"time": 20, "error": "kubectl timed out"}
    assert not m.qualifies_idle(missing, 0, 60, "scale-to-zero")
    assert not m.qualifies_idle(window[3:], 0, 60, "scale-to-zero")


def test_ready_window_requires_one_stable_pod_and_control_plane():
    m = measurements()
    window = [observation(t, desired=1, pods=[pod("warm")]) for t in range(0, 61, 2)]
    assert m.qualifies_idle(window, 0, 60, "keep-ready")
    window[8]["control_plane"][0]["restarts"] = 1
    assert not m.qualifies_idle(window, 0, 60, "keep-ready")


def test_observation_counts_terminating_pods_and_rejects_missing_deployment():
    m = measurements()
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "fn-word-stats-java"},
        "spec": {
            "replicas": 0,
            "selector": {"matchLabels": {"app": "fn-word-stats-java"}},
        },
    }
    old = {
        "kind": "Pod",
        "metadata": {
            "name": "old",
            "uid": "old",
            "deletionTimestamp": "now",
            "labels": {"app": "fn-word-stats-java"},
        },
        "status": {
            "containerStatuses": [
                {
                    "name": "function",
                    "ready": False,
                    "restartCount": 0,
                    "imageID": "sha256:fn",
                }
            ]
        },
    }
    result = m.parse_observation({"items": [deployment, old]}, "word-stats-java", 12)
    assert result["pods"][0]["deleting"] is True
    assert result["desired"] == 0
    with pytest.raises(ValueError, match="Deployment"):
        m.parse_observation({"items": [old]}, "word-stats-java", 12)


def test_wake_success_requires_new_uid_and_matching_image_not_just_http_200():
    m = measurements()
    before = observation(0)
    after = observation(1, desired=1, pods=[pod()])
    assert m.qualifies_response(before, after, "scale-to-zero", {"old"}, "sha256:fn")
    assert not m.qualifies_response(
        before, after, "scale-to-zero", {"new"}, "sha256:fn"
    )
    assert not m.qualifies_response(
        before, after, "scale-to-zero", {"old"}, "sha256:other"
    )


def test_measurement_failure_keeps_summary_and_attempts_metric_collection(
    tmp_path, monkeypatch
):
    import json

    from sonata_engine import TaskInputs

    from nanolab.config.idle_reactivation import IdleReactivationConfig

    class BrokenExecutor:
        def run(self, task, *, dry_run=False):
            raise RuntimeError("loadgen unavailable")

    class Prometheus:
        def query_range(self, *args):
            return [
                {
                    "timestamp": "2026-10-31T00:00:00+00:00",
                    "value": 1,
                    "labels": {"pod": "cp"},
                }
            ]

    module = importlib.import_module("nanolab.tasks.idle_reactivation_runtime")
    monkeypatch.setattr(module.CapturePrometheusSnapshot, "_WINDOW_MARGIN_S", 0)
    output = tmp_path / "arm"
    task = module.RunIdleReactivation(
        function="word-stats-java",
        treatment="scale-to-zero",
        namespace="nanofaas",
        protocol=IdleReactivationConfig(),
        executor=BrokenExecutor(),
        prometheus_client=Prometheus(),
        fetcher=None,
        run_dir=output,
        remote_home=tmp_path / "remote",
        control_plane_url="http://stack:30080",
    )
    outcome = task.run(TaskInputs.empty())
    summary = json.loads((output / "summary.json").read_text())
    assert summary["complete"] is False
    assert summary["not_attempted"] == 3
    assert "loadgen unavailable" in " ".join(summary["errors"])
    assert (output / "metrics/prometheus-snapshot.json").is_file()
    assert outcome.value == summary


def test_idle_memory_drops_stale_function_samples_only_with_verified_zero():
    module = importlib.import_module("nanolab.tasks.idle_reactivation_report")
    samples = [observation(t) for t in range(0, 61, 2)]
    for sample in samples:
        sample["control_plane"][0]["name"] = "cp-pod"
    queries = {}
    for metric in ("container_memory_bytes", "container_rss_bytes"):
        queries[metric + "@control-plane"] = {
            "points": [
                {"timestamp": t, "value": 100 * 1024**2, "labels": {"pod": "cp-pod"}}
                for t in range(0, 61, 5)
            ]
        }
        queries[metric + "@word-stats-java"] = {
            "points": [
                {"timestamp": t, "value": 200 * 1024**2, "labels": {"pod": "old-pod"}}
                for t in range(0, 61, 5)
            ]
        }
    queries["scrape_health"] = {
        "points": [
            {"timestamp": t, "value": 1, "labels": {"job": "kubernetes-cadvisor"}}
            for t in range(0, 61, 5)
        ]
    }
    result = module.memory_window(queries, samples, 0, 60, "word-stats-java")
    assert result["working_set"]["mean_mib"] == 100
    assert result["working_set"]["mib_seconds"] == 6000
    queries["scrape_health"]["points"][3]["value"] = 0
    assert module.memory_window(queries, samples, 0, 60, "word-stats-java") is None


def test_memory_does_not_sum_duplicate_or_unlabelled_container_samples():
    module = importlib.import_module("nanolab.tasks.idle_reactivation_report")
    assert module.memory_window({}, [], 0, 60, "word-stats-java") is None


def test_memory_window_never_interpolates_post_wake_memory_into_idle():
    from nanolab.tasks.idle_reactivation_report import _integral

    assert _integral([(0, 100), (5, 100), (10, 10000)], 1, 9) == 800
    assert _integral([(0, 100), (15, 100)], 0, 15) is None


def test_duplicate_and_unlabelled_pod_memory_is_unknown():
    from nanolab.tasks.idle_reactivation_report import _one

    point = {"value": 100, "labels": {"pod": "cp"}}
    assert _one([point, point], "cp") is None
    assert _one([{"value": 100}], "cp") is None


def test_offline_request_validation_detects_tampered_body_and_missing_attempt(tmp_path):
    import base64
    import json

    from nanolab.tasks.idle_reactivation_report import verify_ledger

    records = []
    fixtures = [{"name": f"case-{i}", "expected": i} for i in range(4)]
    for i in range(6):
        identity = {
            "request_id": str(i),
            "phase": "cycle",
            "cycle": 0,
            "part": i,
            "function": "word-stats-java",
            "case_name": "case-0",
        }
        records += [
            dict(identity, event="started"),
            dict(
                identity,
                event="response",
                status=200,
                error=None,
                valid=True,
                started_monotonic_ns=100,
                ended_monotonic_ns=200,
                duration_ms=0.0001,
                body_base64=base64.b64encode(
                    b'{"status":"success","output":0}'
                ).decode(),
            ),
        ]
    path = tmp_path / "cycle-0000.jsonl"

    def save():
        path.write_text("\n".join(json.dumps(r) for r in records))

    save()
    assert verify_ledger(path, fixtures, "word-stats-java", "cycle", 0, set())
    records[1]["body_base64"] = base64.b64encode(
        b'{"status":"success","output":1}'
    ).decode()
    save()
    assert not verify_ledger(path, fixtures, "word-stats-java", "cycle", 0, set())
    records.pop()
    save()
    assert not verify_ledger(path, fixtures, "word-stats-java", "cycle", 0, set())


def test_burst_rejects_replacement_between_first_response_and_followups():
    from nanolab.tasks.idle_reactivation import qualifies_burst

    samples = [
        observation(0),
        observation(2, desired=1, pods=[pod("first")]),
        observation(4, desired=1, pods=[pod("replacement")]),
    ]
    assert not qualifies_burst(samples, 0, 4, samples[-1])
    assert qualifies_burst(samples[:2], 0, 2, samples[1])


def test_offline_report_retains_missing_arms_as_incomplete(tmp_path):
    from nanolab.tasks.idle_reactivation_report import analyze_pair

    report = analyze_pair(tmp_path)
    assert report["passed"] is False
    assert set(report["arms"]) == {"keep-ready", "scale-to-zero"}
    assert all(arm["errors"] for arm in report["arms"].values())
    assert (tmp_path / "paired-summary.json").is_file()


def test_offline_lifecycle_rechecks_raw_observations_outside_idle_windows():
    from nanolab.tasks.idle_reactivation_report import verify_lifecycle

    samples = [observation(t) for t in range(0, 363, 2)]
    samples[0] = observation(0, desired=1, pods=[pod("old")])
    samples[91] = observation(182, desired=1, pods=[pod("new")])
    for sample in samples:
        sample["control_plane"][0]["ready"] = True
    summary = {
        "measurement_start": 0,
        "configured_cycles": 1,
        "treatment": "scale-to-zero",
        "complete": True,
        "cycles": [{"cycle": 0, "idle_end": 180, "end": 182}],
        "tail": {"end": 362},
    }
    assert verify_lifecycle(summary, samples)
    samples[20]["control_plane"][0]["uid"] = "replaced"
    assert not verify_lifecycle(summary, samples)


@pytest.mark.parametrize("operation", ["observe", "probe"])
def test_remote_measurement_commands_preserve_deadlines_with_vm_executor(
    tmp_path, operation
):
    import json

    from sonata_tasks.execution.adapters import VmCommandTaskExecutor

    from nanolab.config.idle_reactivation import IdleReactivationConfig
    from nanolab.tasks.idle_reactivation_runtime import (
        LifecycleWatcher,
        RunIdleReactivation,
    )
    from tests.cli.test_execution_bindings import _Result
    from tests.plans.test_loadtest import NoopPrometheus

    calls = []
    records = []
    for part in range(6):
        identity = {
            "request_id": str(part),
            "phase": "cycle",
            "cycle": 0,
            "part": part,
            "function": "word-stats-java",
        }
        records.extend(
            [
                dict(identity, event="started"),
                dict(identity, event="response", valid=True),
            ]
        )

    class Runner:
        def run_vm_command(self, argv, **kwargs):
            calls.append(argv)
            if "kubectl" in argv:
                stdout = json.dumps(
                    {
                        "items": [
                            {
                                "kind": "Deployment",
                                "metadata": {"name": "fn-word-stats-java"},
                                "spec": {
                                    "replicas": 0,
                                    "selector": {
                                        "matchLabels": {"app": "fn-word-stats-java"}
                                    },
                                },
                            }
                        ]
                    }
                )
            else:
                stdout = "\n".join(json.dumps(r) for r in records)
            return _Result(stdout=stdout)

    executor = VmCommandTaskExecutor(Runner(), target_key="azure:test")
    if operation == "observe":
        watcher = LifecycleWatcher(
            executor, "word-stats-java", "nanofaas", tmp_path / "lifecycle.jsonl"
        )
        sample = watcher.observe()
        assert "error" not in sample, sample
        assert sample["desired"] == 0
    else:
        task = RunIdleReactivation(
            "word-stats-java",
            "scale-to-zero",
            "nanofaas",
            IdleReactivationConfig(),
            executor,
            NoopPrometheus(),
            None,
            tmp_path,
            tmp_path / "remote",
            "http://stack:30080",
        )
        assert len(task._probe("cycle")) == 6
    assert calls[0][:3] == (
        "timeout",
        "--kill-after=5s",
        "15s" if operation == "observe" else "300s",
    )


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("log_failure", [False, True])
def test_finalization_collects_bounded_vm_logs_and_preserves_measurement_failure(
    tmp_path, monkeypatch, interrupted, log_failure
):
    import json

    from sonata_engine import TaskInputs
    from sonata_tasks.execution.adapters import VmCommandTaskExecutor

    from nanolab.config.idle_reactivation import IdleReactivationConfig
    from nanolab.plans.diagnostics import collect_control_plane_log
    from tests.cli.test_execution_bindings import _Result
    from tests.plans.test_loadtest import NoopPrometheus

    module = importlib.import_module("nanolab.tasks.idle_reactivation_runtime")
    calls = []

    class Runner:
        def run_vm_command(self, argv, **kwargs):
            calls.append(argv)
            return _Result(
                stdout="2026-10-31T12:00:00Z Scaling DOWN echo\n",
                stderr="log endpoint unavailable" if log_failure else "",
                return_code=1 if log_failure else 0,
            )

    def fail_measurement(self, watcher, report):
        if interrupted:
            raise KeyboardInterrupt()
        raise RuntimeError("measurement failed")

    monkeypatch.setattr(module.LifecycleWatcher, "start", lambda self: None)
    monkeypatch.setattr(module.LifecycleWatcher, "stop", lambda self: None)
    monkeypatch.setattr(module.RunIdleReactivation, "_measure", fail_measurement)
    monkeypatch.setattr(module.CapturePrometheusSnapshot, "_WINDOW_MARGIN_S", 0)
    executor = VmCommandTaskExecutor(Runner(), target_key="azure:stack")
    output = tmp_path / "arm"
    task = module.RunIdleReactivation(
        "word-stats-java",
        "scale-to-zero",
        "nanofaas",
        IdleReactivationConfig(),
        executor,
        NoopPrometheus(),
        None,
        output,
        tmp_path / "remote",
        "http://stack:30080",
        control_plane_log=collect_control_plane_log(
            executor=executor,
            role="stack",
            namespace="nanofaas",
            destination=output / "control-plane.log",
        ),
    )
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            task.run(TaskInputs({}, frozenset()))
    else:
        task.run(TaskInputs({}, frozenset()))
    summary = json.loads((output / "summary.json").read_text())
    assert summary["complete"] is False
    assert ("KeyboardInterrupt" if interrupted else "measurement failed") in summary[
        "errors"
    ]
    assert calls == [
        (
            "timeout",
            "--kill-after=5s",
            "30s",
            "sudo",
            "kubectl",
            "--request-timeout=10s",
            "-n",
            "nanofaas",
            "logs",
            "deploy/nanofaas-control-plane",
            "--tail=-1",
            "--timestamps=true",
        )
    ]
    if log_failure:
        assert any("log endpoint unavailable" in error for error in summary["errors"])
    else:
        assert (
            (output / "control-plane.log").read_text().endswith("Scaling DOWN echo\n")
        )
