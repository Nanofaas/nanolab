import pytest

from nanolab.config.one_shot import ArtifactReference
from nanolab.tasks.one_shot.experiment import read_qualification


def test_changed_timing_artifact_is_refused_before_load(tmp_path):
    from types import SimpleNamespace

    path = tmp_path / "timing.json"
    path.write_text("{}")
    settings = SimpleNamespace(
        qualification=ArtifactReference(path=path, sha256="a" * 64)
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        read_qualification(settings)


def test_plan_passes_preflight_and_evidence_through_explicit_step_scopes(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from typing import Any, cast

    from sonata_engine import Resource, Task, TaskOutcome

    from nanolab.plans.one_shot_experiment import build_one_shot_experiment_plan

    target = "nanolab.plans.one_shot_experiment."
    calls = []
    resource = Resource(title="fake", acquire=lambda _: None, release=lambda *_: None)
    settings = SimpleNamespace(
        provider="multipass",
        purpose="workflow-validation",
        seed=7,
        flow_quantum=1,
        functions={"f": SimpleNamespace(input={})},
        nodes=[
            SimpleNamespace(id="edge-0", kind="edge"),
            SimpleNamespace(id="edge-1", kind="edge"),
            SimpleNamespace(id="cloud", kind="cloud"),
        ],
        experiment=SimpleNamespace(
            windows=[SimpleNamespace(rates={"edge-0": {"f": 4}, "edge-1": {"f": 4}})],
            repetitions=1,
            generator_vus=2,
        ),
    )
    scenario = SimpleNamespace(
        workflow="one-shot-experiment", one_shot=settings, functions=["f"]
    )
    qualification = SimpleNamespace(
        period_seconds=20,
        matrix=[
            {
                "cell": {
                    "id": "balanced",
                    "rates": {"edge-0": {"f": 4}, "edge-1": {"f": 4}},
                }
            }
        ],
    )
    monkeypatch.setattr(target + "read_profile", lambda _: object())
    monkeypatch.setattr(target + "read_qualification", lambda _: qualification)
    monkeypatch.setattr(
        target + "resolve_function_definition", lambda *_: SimpleNamespace(family="f")
    )
    monkeypatch.setattr(
        target + "add_one_shot_platforms",
        lambda *args, **kwargs: SimpleNamespace(
            requires=(resource,),
            endpoints=dict.fromkeys(["edge-0", "edge-1", "cloud"], resource),
            probes={},
        ),
    )
    monkeypatch.setattr(target + "clock_health_resource", lambda **_: resource)
    monkeypatch.setattr(target + "generator_resource", lambda **_: resource)
    monkeypatch.setattr(target + "evidence_collector_resource", lambda *_: resource)

    class Preflight(Task):
        title = "preflight"

        def __init__(self, *args, **kwargs):
            pass

        def run(self, inputs):
            return TaskOutcome(value="preflight-evidence")

    class Freeze(Preflight):
        title = "freeze"

        def run(self, inputs):
            assert inputs.upstream() == "preflight-evidence"
            calls.append("frozen")
            return TaskOutcome()

    class Empty(Preflight):
        title = "empty"

        def run(self, inputs):
            return TaskOutcome()

    class Collect(Preflight):
        title = "collect"

        def run(self, inputs):
            return TaskOutcome(value="run-evidence")

    class Evaluate(Preflight):
        title = "evaluate"

        def run(self, inputs):
            assert inputs.upstream() == "run-evidence"
            calls.append("evaluated")
            return TaskOutcome()

    for name, task in [
        ("CollectTopologyTask", Preflight),
        ("FreezeManifestTask", Freeze),
        ("UploadForecastTask", Empty),
        ("RunOneShotLoadTask", Empty),
        ("CollectEvidenceTask", Collect),
        ("EvaluateOneShotTask", Evaluate),
    ]:
        monkeypatch.setattr(target + name, task)
    topology = SimpleNamespace(resources=SimpleNamespace())
    monkeypatch.setattr(
        target + "add_one_shot_platforms",
        lambda *args, **kwargs: SimpleNamespace(
            requires=(resource,),
            resources=topology.resources,
            endpoints=dict.fromkeys(["edge-0", "edge-1", "cloud"], resource),
            probes={},
            distribution=resource,
            function_settings={},
        ),
    )
    build_one_shot_experiment_plan(
        cast(Any, scenario), cast(Any, object()), run_dir=tmp_path, repo_root=tmp_path
    ).run()
    assert calls == ["frozen", "evaluated"] * 3
