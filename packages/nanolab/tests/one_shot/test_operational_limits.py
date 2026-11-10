"""Observation limits never change the physical experiment resources."""

import importlib.util
from pathlib import Path

from nanolab.tasks.resource_tasks import acquire_now


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
    from pathlib import Path
    from types import SimpleNamespace

    from sonata_engine import TaskInputs
    from sonata_tasks.ansible import AnsiblePlaybookTask

    from nanolab.config.one_shot import OperationalLimits
    from nanolab.workbench import components, loadgen
    from nanolab.workbench.platform import OPERATOR
    from tests.workbench.playbook_fakes import host_of

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
    monkeypatch.setattr(loadgen.time, "monotonic", lambda: 900)
    monkeypatch.setattr(components, "ensure_installed", lambda: Path("/coll"))
    environments = []

    def deploy(task, inputs):
        role = host_of(task)
        saved = tmp_path / "workbench" / "vars" / f"{role}-deploy.json"
        environments.append(json.loads(saved.read_text())["instances"][0]["config"])

    monkeypatch.setattr(AnsiblePlaybookTask, "run", deploy)
    resource = loadgen.loadgen_resource(
        OPERATOR,
        run_dir=tmp_path,
        vus=512,
        duration=4050,
        settings=SimpleNamespace(operational_limits=OperationalLimits()),
    )
    acquire_now(resource, TaskInputs._for_resource_values({}, ()))
    assert environments[0]["env"]["ONE_SHOT_DURATION"] == "3520s"
