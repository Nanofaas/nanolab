import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nanolab.one_shot.models import CampaignManifest
from nanolab.tasks.one_shot.experiment import RunOneShotLoadTask, generator_rows


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
        anchor=datetime.now(UTC).isoformat(),
    )


@pytest.mark.parametrize("collector_fails", [False, True])
def test_load_failure_keeps_original_error_and_collects_even_if_collection_fails(
    tmp_path, monkeypatch, collector_fails
):
    (tmp_path / "manifest.json").write_text(manifest().model_dump_json())
    actions = []

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            actions.append("terminate")
            self.returncode = -15

        def wait(self, **kwargs):
            actions.append("wait")
            return self.returncode

    def close():
        actions.append("collector-close")
        if collector_fails:
            raise RuntimeError("collector cleanup failed")

    collector = SimpleNamespace(close=close)
    freeze = SimpleNamespace(
        run_dir=tmp_path,
        clock="clock",
        settings=SimpleNamespace(experiment=SimpleNamespace(fail_load=True)),
    )
    values = {"generator": Child(), "collector": collector, "clock": object()}
    inputs = cast(Any, SimpleNamespace(resource=lambda key: values[key]))

    def collect(*args):
        actions.append("collect")
        raise RuntimeError("collection refused")

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.collect_runtime_evidence", collect
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.collect_original_evidence",
        lambda *args: actions.append("evidence"),
    )
    with pytest.raises(RuntimeError, match="injected load-task failure"):
        RunOneShotLoadTask(freeze, "generator", "collector").run(inputs)
    assert actions == ["collector-close", "terminate", "wait", "collect", "evidence"]
    assert "injected load-task failure" in (tmp_path / "load-status.jsonl").read_text()
    assert "collection refused" in (tmp_path / "collection-errors.jsonl").read_text()


def test_generator_parser_does_not_treat_empty_or_summary_as_load(tmp_path):
    path = tmp_path / "generator.jsonl"
    assert generator_rows(path) == []
    path.write_text(
        "garbage\n"
        + json.dumps({"msg": json.dumps({"event": "summary"})})
        + "\n"
        + json.dumps({"msg": json.dumps({"event": "emitted", "originalId": "1"})})
        + "\n"
    )
    assert generator_rows(path) == [{"event": "emitted", "originalId": "1"}]


def test_single_attempt_proof_cannot_be_reused_for_two_originals():
    from nanolab.tasks.one_shot.conservation import evaluate_conservation

    planned = [
        {"originalId": str(i), "origin": "edge-0", "function": "work", "epoch": 0}
        for i in range(2)
    ]
    attempts = [
        {
            **row,
            "executionId": "shared",
            "incarnation": "runtime",
            "dispatchAttempt": "1",
            "handlerStarted": True,
            "state": "RELEASED",
            "responseStatus": "SUCCESS",
        }
        for row in planned
    ]
    results = [{**row, "success": True} for row in planned]
    assert not evaluate_conservation(planned, planned, attempts, results).valid


def test_ambiguous_bad_gateway_is_censored_but_explicit_admission_refusal_is_terminal():
    from nanolab.tasks.one_shot.experiment import classify_result

    row = {
        "statusCode": 502,
        "offloadedTarget": "http://peer:8080",
        "responseStatus": None,
        "outputJson": None,
        "responseBody": "upstream unavailable",
    }
    assert not classify_result(row, expected="42", physical=[])["terminalError"]
    row["responseBody"] = (
        '{"error":"OFFLOAD_FAILED",'
        '"message":"remote http://peer:8080 returned 429: queue full"}'
    )
    assert classify_result(row, expected="42", physical=[])["terminalError"]
    row.update(statusCode=200, responseStatus="error")
    assert not classify_result(row, expected="42", physical=[])["terminalError"]
    assert classify_result(row, expected="42", physical=[{"state": "RELEASED"}])[
        "terminalError"
    ]


@pytest.mark.parametrize(
    ("message", "target"),
    [
        ("remote http://peer:8080 returned 500: returned 429", "http://peer:8080"),
        ("remote http://peer:8080 returned 4290: failure", "http://peer:8080"),
        ("remote http://other:8080 returned 429: failure", "http://peer:8080"),
        ("remote http://peer:8080 returned 429: failure", None),
    ],
)
def test_admission_refusal_requires_exact_gateway_target_and_status(message, target):
    import json

    from nanolab.tasks.one_shot.experiment import classify_result

    row = {
        "statusCode": 502,
        "offloadedTarget": target,
        "responseStatus": None,
        "outputJson": None,
        "responseBody": json.dumps({"error": "OFFLOAD_FAILED", "message": message}),
    }
    assert not classify_result(row, expected="42", physical=[])["terminalError"]


@pytest.mark.parametrize("censored", [False, True])
def test_manual_epoch_preparation_is_once_and_rejects_censored_budget(
    tmp_path, monkeypatch, censored
):
    from nanolab.tasks.one_shot.experiment import FreezeManifestTask

    result = manifest().model_copy(update={"mode": "oracle"})
    (tmp_path / "manifest.json").write_text(result.model_dump_json())
    node = SimpleNamespace(config=SimpleNamespace(memory_capacity_mib=128))
    topology = SimpleNamespace(
        endpoints={"edge-0": "endpoint"},
        resources=SimpleNamespace(nodes={"edge-0": node}),
    )
    freeze = cast(
        Any,
        SimpleNamespace(
            run_dir=tmp_path,
            clock="clock",
            mode="oracle",
            topology=topology,
            trace=SimpleNamespace(nodes=["edge-0"], windows=[{}], period_seconds=20),
            settings=SimpleNamespace(experiment=SimpleNamespace(fail_load=False)),
            qualification=SimpleNamespace(margin_seconds=0.2, epsilon=0.1),
        ),
    )
    calls = []

    class Child:
        returncode = None

        def poll(self):
            if calls:
                self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, **_):
            return self.returncode

    collector = SimpleNamespace(require_healthy=lambda: None, close=lambda: None)
    resources = {
        "clock": SimpleNamespace(require_healthy=lambda: None),
        "generator": Child(),
        "collector": collector,
        "endpoint": "http://edge",
    }
    inputs = cast(Any, SimpleNamespace(resource=lambda key: resources[key]))
    plan = {
        "flowQuantum": 1,
        "functions": {
            "work": {
                "readyReplicas": 1,
                "memoryMiB": 128,
                "localRate": 4,
                "forecastRate": 4,
                "cloudRate": 0,
                "demandSeconds": 0.1,
                "utilization": 0.8,
                "inbound": [],
                "outbound": [],
            }
        },
    }

    def prepare(*_, **kwargs):
        calls.append(kwargs["epoch"])
        return SimpleNamespace(
            censored=censored, ready_seconds=0.1, auction_seconds=0.1
        ), {"nodes": {"edge-0": {"result": {"plan": plan}}}}

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.prepare_parallel_epoch", prepare
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.wait_until", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.collect_runtime_evidence",
        lambda *_args: None,
    )
    task = RunOneShotLoadTask(
        cast(FreezeManifestTask, freeze), "generator", "collector"
    )
    if censored:
        with pytest.raises(RuntimeError, match="qualified wall"):
            task.run(inputs)
    else:
        task.run(inputs)
    assert calls == [0]


def test_proxy_admission_error_is_terminal_without_an_invented_sdk_receipt():
    from nanolab.tasks.one_shot.experiment import classify_result

    row = {
        "statusCode": 200,
        "responseStatus": "error",
        "outputJson": None,
        "responseBody": json.dumps(
            {
                "error": {
                    "code": "EXTERNAL_ERROR",
                    "message": "Too many concurrent invocations",
                }
            }
        ),
    }
    assert classify_result(row, expected="7", physical=[])["terminalError"]
    row["responseBody"] = json.dumps(
        {
            "error": {
                "code": "EXTERNAL_ERROR",
                "message": "Proxy timed out forwarding the invocation",
            }
        }
    )
    assert not classify_result(row, expected="7", physical=[])["terminalError"]


def test_artifact_write_failure_keeps_primary_load_error_and_attempts_cleanup(
    tmp_path, monkeypatch
):
    (tmp_path / "manifest.json").write_text(manifest().model_dump_json())
    actions = []

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            actions.append("terminate")
            self.returncode = -15

        def wait(self, **kwargs):
            actions.append("wait")
            return self.returncode

    freeze = SimpleNamespace(
        run_dir=tmp_path,
        clock="clock",
        settings=SimpleNamespace(experiment=SimpleNamespace(fail_load=True)),
    )
    values = {
        "generator": Child(),
        "collector": SimpleNamespace(close=lambda: actions.append("close")),
        "clock": object(),
    }
    inputs = cast(Any, SimpleNamespace(resource=lambda key: values[key]))

    def write(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("nanolab.tasks.one_shot.experiment.append_observation", write)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.collect_runtime_evidence",
        lambda *args: actions.append("collect"),
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.collect_original_evidence",
        lambda *args: actions.append("evidence"),
    )
    with pytest.raises(RuntimeError, match="injected load-task failure") as error:
        RunOneShotLoadTask(freeze, "generator", "collector").run(inputs)
    assert actions == ["close", "terminate", "wait", "collect", "evidence"]
    assert any("No space left" in note for note in error.value.__notes__)
