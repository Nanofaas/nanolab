from pathlib import Path

import pytest

from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.plans.one_shot_calibration import build_one_shot_calibration_plan


def scenario():
    return ScenarioConfig.model_validate(
        {
            "workflow": "one-shot-calibration",
            "backend": "container",
            "functions": ["one-shot-workload-rust"],
            "oneShot": {
                "provider": "multipass",
                "purpose": "workflow-validation",
                "nodes": [
                    {"id": "edge-0", "kind": "edge"},
                    {"id": "edge-1", "kind": "edge"},
                    {"id": "cloud", "kind": "cloud"},
                ],
                "functions": {
                    "one-shot-workload-rust": {
                        "input": {
                            "iterations": 100,
                            "working_set_bytes": 4096,
                            "seed": 7,
                        }
                    }
                },
            },
        }
    )


def environment():
    return EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )


def test_calibration_compiles_without_provisioning(tmp_path, monkeypatch):
    (tmp_path / "functions/rust/one-shot-workload").mkdir(parents=True)
    monkeypatch.setenv("NANOFAAS_ROOT", str(tmp_path))
    workflow = build_one_shot_calibration_plan(
        scenario(),
        environment(),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
    )
    compiled = workflow.compile()
    titles = [entry.task.title for entry in compiled.tasks]
    assert any("calibration" in title.lower() for title in titles)
    assert not (tmp_path / "run/profile.json").exists()


def test_wrong_workflow_is_rejected_before_provisioning(tmp_path):
    with pytest.raises(ValueError, match="calibration"):
        build_one_shot_calibration_plan(
            scenario().model_copy(update={"workflow": "validate"}),
            environment(),
            run_dir=tmp_path,
            repo_root=Path.cwd(),
        )


def test_logging_compiles_one_collector_for_every_node(tmp_path,monkeypatch):
    data=scenario().model_dump(by_alias=True,exclude_unset=True)
    data['oneShot']['functions']['one-shot-workload-rust']['cpu']=.5
    data['oneShot']['observability']={'logs':{'enabled':True,'endpoint':'http://10.0.0.1:3100/loki/api/v1/push'}}
    config=ScenarioConfig.model_validate(data)
    (tmp_path/'functions/rust/one-shot-workload').mkdir(parents=True)
    assets=tmp_path/'deploy/observability';assets.mkdir(parents=True)
    for filename in ['fluent-bit.conf','parsers.conf']:(assets/filename).write_text(filename)
    monkeypatch.setenv('NANOFAAS_ROOT',str(tmp_path))
    workflow=build_one_shot_calibration_plan(config,environment(),run_dir=tmp_path/'run',repo_root=tmp_path)
    compiled=workflow.compile()
    # Resource dependency closure, rather than constructing unused handles.
    titles=[unit.resource.title for unit in compiled.tasks if unit.kind=="acquire" and unit.resource is not None]
    assert sum('Fluent Bit collector' in title for title in titles)==3
    assert 'Use external logging backend' in titles
