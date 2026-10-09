import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import yaml

from nanolab.config.one_shot import ArtifactReference, OneShotConfig
from nanolab.one_shot.contracts import contract_asset
from nanolab.one_shot.models import CalibrationProfile, TimingQualification
from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash
from nanolab.tasks.one_shot.experiment import FreezeManifestTask, UploadForecastTask
from nanolab.tasks.one_shot.trace import TraceArtifact


def frozen_run(tmp_path, monkeypatch, *, mode="oracle"):
    preset = (
        Path(__file__).parents[2]
        / "src/nanolab/assets/presets/scenarios/one-shot-experiment.yaml"
    )
    settings = OneShotConfig.model_validate(
        yaml.safe_load(preset.read_text())["oneShot"]
    )
    doc = json.loads(contract_asset("examples/synthetic-profile.json").read_bytes())
    doc.update(provider="multipass", synthetic=False)
    doc["functions"] = [doc["functions"][0]]
    doc["functions"][0].update(function="one-shot-workload", serviceSeconds=0.1)
    profile = CalibrationProfile.model_validate(doc)
    settings.profile = ArtifactReference(
        path=tmp_path / "profile.json", sha256=content_hash(canonical_bytes(doc))
    )
    settings.qualification = ArtifactReference(
        path=tmp_path / "timing.json", sha256="c" * 64
    )
    qualification = TimingQualification(
        provider="multipass",
        purpose="workflow-validation",
        environment_fingerprint=profile.environment_fingerprint,
        profile_sha256=settings.profile.sha256,
        qualified=True,
        period_seconds=20,
        lead_seconds=1.9,
        quantile=0.9,
        quantile_seconds=0.1,
        margin_seconds=0.2,
        epsilon=0.1,
        sample_count=10,
        censored_count=0,
        samples_seconds=[0.1] * 10,
        protocol={"preparationSeconds": 1},
    )
    nodes = {node.id: SimpleNamespace(config=node) for node in settings.nodes}
    topology = SimpleNamespace(
        resources=SimpleNamespace(nodes=nodes),
        endpoints={key: key for key in nodes},
        distribution="distribution",
        function_settings={
            "one-shot-workload": settings.functions["one-shot-workload-rust"]
        },
    )
    built = SimpleNamespace(
        source={"revision": profile.source_commit, "dirty": False},
        components=[
            SimpleNamespace(
                name="control-plane", image=SimpleNamespace(id="sha256:" + "a" * 64)
            )
        ],
    )
    values = {
        "distribution": built,
        "clock": SimpleNamespace(require_healthy=lambda: None),
        **{key: "http://" + key for key in nodes},
    }
    inputs = cast(
        Any,
        SimpleNamespace(resource=lambda key: values[key], upstream=lambda: object()),
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.verify_profile_scope",
        lambda *args: profile.environment_fingerprint,
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="k6 fake-test"),
    )
    trace = TraceArtifact(
        nodes=["edge-0", "edge-1"],
        functions=["one-shot-workload"],
        period_seconds=20,
        flow_quantum=1,
        seed=7,
        payloads={
            "one-shot-workload": settings.functions["one-shot-workload-rust"].input
        },
        windows=[
            {"edge-0": {"one-shot-workload": 4}, "edge-1": {"one-shot-workload": 4}},
            {"edge-0": {"one-shot-workload": 15}, "edge-1": {"one-shot-workload": 0}},
        ],
    )
    run = FreezeManifestTask(
        topology,
        settings,
        profile,
        qualification,
        trace,
        mode=mode,
        repetition=0,
        modes=["baseline", "oracle", "ewma"],
        clock="clock",
        run_dir=tmp_path,
    )
    return run, inputs


def test_manifest_freezes_same_trace_and_distinct_originals_before_load(
    tmp_path, monkeypatch
):
    run, inputs = frozen_run(tmp_path, monkeypatch)
    manifest = run.run(inputs).value
    assert manifest is not None and manifest.trace_sha256 == run.trace.sha256
    rows = json.loads((tmp_path / "schedule.json").read_bytes())
    assert sum(row["phase"] == "campaign" for row in rows) == 460
    assert sum(row["phase"] == "warmup" for row in rows) == 40
    assert len({row["originalId"] for row in rows}) == 500
    generator = manifest.parameters["generator"]
    assert isinstance(generator, dict) and generator["retry"] is False
    assert manifest.provider == "multipass"
    with pytest.raises(ValueError, match="immutable"):
        run.run(inputs)


def test_timing_fingerprint_or_terminal_headroom_failure_prevents_manifest(
    tmp_path, monkeypatch
):
    run, inputs = frozen_run(tmp_path, monkeypatch)
    run.qualification.environment_fingerprint = "other"
    with pytest.raises(ValueError, match="fingerprint"):
        run.run(inputs)
    assert not (tmp_path / "manifest.json").exists()
    run.qualification.environment_fingerprint = run.profile.environment_fingerprint
    run.profile.functions[0]["serviceSeconds"] = 1
    with pytest.raises(ValueError, match="headroom"):
        run.run(inputs)
    assert not (tmp_path / "schedule.json").exists()


@pytest.mark.parametrize("mode", ["baseline", "oracle", "ewma"])
def test_mode_uses_expected_public_apis_and_never_injects_decisions(
    tmp_path, monkeypatch, mode
):
    run, inputs = frozen_run(tmp_path, monkeypatch, mode=mode)
    run.run(inputs)
    actions = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (
                actions.append((request.method, str(request.url), request.content))
                or httpx.Response(200)
            )
        )
    ) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.httpx.Client",
            lambda **kwargs: nullcontext(http),
        )
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.runtime_endpoint",
            lambda *args, **kwargs: ("http://runtime", {}),
        )

        class Client:
            def __init__(self, url, **kwargs):
                self.base_url = url

            def load_profile(self, *args, **kwargs):
                actions.append(("PROFILE", self.base_url, None))

            def status(self):
                return SimpleNamespace(
                    revision=1, catalog_generations={"one-shot-workload": 1}
                )

            def configure(self, conf, **kwargs):
                actions.append(("CONFIGURE", self.base_url, conf))

            def load_trace(self, trace, **kwargs):
                actions.append(("TRACE", self.base_url, trace))
                return SimpleNamespace(model_dump=lambda **_: {"revision": 1})

        monkeypatch.setattr(
            "nanolab.tasks.one_shot.experiment.NanoFaasOneShotClient", Client
        )
        UploadForecastTask(run).run(inputs)
    assert len([action for action in actions if action[0] == "TRACE"]) == (
        2 if mode == "oracle" else 0
    )
    assert len([action for action in actions if action[0] == "CONFIGURE"]) == (
        0 if mode == "baseline" else 2
    )
    assert not [action for action in actions if action[0] == "PATCH"]


@pytest.mark.parametrize("missing_proof", [False, True])
@pytest.mark.parametrize("load_failed", [False, True])
def test_collected_evidence_counts_real_events_and_never_fills_missing_sdk_proof(
    tmp_path, monkeypatch, missing_proof, load_failed
):
    from nanolab.tasks.one_shot.experiment import (
        CollectEvidenceTask,
        EvaluateOneShotTask,
    )

    run, inputs = frozen_run(tmp_path, monkeypatch)
    function = run.topology.function_settings["one-shot-workload"]
    function.input = {"iterations": 0, "working_set_bytes": 0, "seed": 7}
    run.trace.payloads["one-shot-workload"] = {
        "iterations": 0,
        "working_set_bytes": 0,
        "seed": 7,
    }
    run.run(inputs)
    rows = [
        row
        for row in json.loads((tmp_path / "schedule.json").read_bytes())
        if row["phase"] == "campaign"
    ]
    events = []
    proofs = []
    for index, row in enumerate(rows):
        result = {
            **row,
            "event": "result",
            "statusCode": 200,
            "responseStatus": "success",
            "outputJson": "7",
            "responseBody": "{}",
            "executionNode": row["origin"],
            "offloadedTarget": None,
            "executionId": f"execution-{index}",
            "incarnation": "fake-runtime",
            "dispatchAttempt": "1",
            "emittedAt": row["scheduledAt"],
            "latencySeconds": 0.1,
            "finishedAt": row["scheduledAt"] + 0.1,
        }
        events += [{**result, "event": "emitted"}, result]
        if not missing_proof or index:
            proofs.append(
                {
                    **result,
                    "destination": row["origin"],
                    "state": "RELEASED",
                    "handlerStarted": True,
                    "occupancySeconds": 0.1,
                }
            )
    for warm in json.loads((tmp_path / "schedule.json").read_bytes()):
        if warm["phase"] == "warmup":
            outcome = {
                **events[-1],
                **warm,
                "emittedAt": warm["scheduledAt"],
                "finishedAt": warm["scheduledAt"] + 0.1,
            }
            events += [{**outcome, "event": "emitted"}, outcome]
    (tmp_path / "generator.jsonl").write_text(
        "".join(json.dumps({"msg": json.dumps(row)}) + "\n" for row in events)
    )
    (tmp_path / "physical.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in proofs)
    )
    (tmp_path / "runtime-inventory.jsonl").write_text(
        json.dumps(
            {
                "node": "edge-0",
                "at": "sample",
                "function": "one-shot-workload",
                "status": {
                    "maxConcurrentHandlers": 1,
                    "activeHandlers": 0,
                    "physicalReleaseProof": True,
                },
                "container": {
                    "HostConfig": {"Memory": 128 * 1024 * 1024, "NanoCpus": 1000000000}
                },
            }
        )
        + "\n"
    )
    if load_failed:
        (tmp_path / "load-status.jsonl").write_text(
            json.dumps({"failure": "late protocol failure", "exitCode": 0}) + "\n"
        )
    evidence = CollectEvidenceTask(run).run(inputs).value
    assert evidence is not None
    assert evidence.completed == (459 if missing_proof else 460)
    assert evidence.censored == (1 if missing_proof else 0)
    assert evidence.valid == (not missing_proof and not load_failed)
    evaluated = cast(Any, SimpleNamespace(upstream=lambda: evidence))
    if missing_proof or load_failed:
        with pytest.raises(ValueError, match="nonconforming"):
            EvaluateOneShotTask().run(evaluated)
    else:
        assert EvaluateOneShotTask().run(evaluated).value is evidence
        assert evidence.observations["realizedUtility"] == 1
    assert (tmp_path / "evidence.json").exists()


def test_long_warmup_is_fully_in_the_future(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    run, inputs = frozen_run(tmp_path, monkeypatch)
    assert run.settings.experiment is not None
    run.settings.experiment.warmup_seconds = 60
    before = datetime.now(UTC).timestamp()
    run.run(inputs)
    warm = [
        row
        for row in json.loads((tmp_path / "schedule.json").read_bytes())
        if row["phase"] == "warmup"
    ]
    assert len(warm) == 480
    assert min(row["scheduledAt"] for row in warm) > before + 10
