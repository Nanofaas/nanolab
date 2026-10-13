"""Observation limits never change the physical experiment resources."""

import importlib.util
from pathlib import Path
from typing import Any, cast


def test_observation_binding_centralizes_limits_without_changing_hardware(tmp_path):
    path = Path(__file__).parents[4] / "scripts/one-shot-observe.py"
    spec = importlib.util.spec_from_file_location("limits_observer", path)
    assert spec is not None and spec.loader is not None
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    distribution = tmp_path / "distribution.json"
    distribution.write_text("{}")
    source = {
        "oneShot": {
            "nodes": [{"id": "a", "cpus": 1, "memoryMiB": 2048}],
            "functions": {"f": {"cpu": 0.1, "memoryMiB": 64}},
            "timing": {"matrix": []},
        }
    }
    bound = observer.bind_scenario(source, distribution)["oneShot"]
    assert bound["nodes"] == source["oneShot"]["nodes"]
    assert bound["functions"] == source["oneShot"]["functions"]
    assert bound["timing"]["protocol"]["auctionSeconds"] == 60
    assert bound["timing"]["protocol"]["peerSeconds"] == 5
    assert bound["timing"]["protocol"]["solverSeconds"] == 5
    assert bound["timing"]["protocol"]["preparationSeconds"] == 10
    assert bound["operationalLimits"]["apiSeconds"] == 180
    assert bound["operationalLimits"]["generatorDrainSeconds"] == 180
    assert (
        observer.oracle_lifetime_seconds(
            type("Trace", (), {"windows": [{}] * 9, "period_seconds": 360})(),
            warmup_seconds=720,
        )
        == 4230
    )


def test_limits_are_optional_and_legacy_defaults_remain_explicit():
    from nanolab.config.one_shot import OperationalLimits, operational_limit

    assert operational_limit(None, "api_seconds", 30) == 30
    limits = OperationalLimits()
    settings = type("Settings", (), {"operational_limits": limits})()
    assert operational_limit(settings, "api_seconds", 30) == 180
    assert operational_limit(settings, "process_exit_seconds", 5) == 30


def test_generator_duration_uses_remaining_frozen_horizon(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from sonata_engine import TaskInputs

    from nanolab.config.one_shot import OperationalLimits
    from nanolab.tasks.one_shot import experiment

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "period_seconds": 360,
                "parameters": {
                    "logicalSchedule": {"anchorMonotonicSeconds": 1000, "epochCount": 9}
                },
            }
        )
    )
    monkeypatch.setattr(experiment.time, "monotonic", lambda: 900)
    environments = []
    child = SimpleNamespace(poll=lambda: 0)

    def start(*_, **kwargs):
        environments.append(kwargs["env"])
        return child

    monkeypatch.setattr(experiment.subprocess, "Popen", start)
    resource = experiment.generator_resource(
        run_dir=tmp_path,
        vus=512,
        duration=4050,
        settings=SimpleNamespace(operational_limits=OperationalLimits()),
    )
    inputs = TaskInputs._for_resources({}, set())
    resource.acquire(inputs)
    resource.release(inputs, cast(Any, child))
    assert environments[0]["ONE_SHOT_DURATION"] == "3520s"
