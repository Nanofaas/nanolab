import json

import pytest
import yaml
from sonata_tasks.execution.bindings import RoleBindings

from nanolab.comparison.module_cost import prepare_campaign
from nanolab.config import EnvironmentConfig, ScenarioConfig

from .test_loadtest import NoopPrometheus, RecordingExecutor


def prepared(tmp_path):
    root = prepare_campaign(tmp_path, tmp_path / "key.pub", 5)
    block = root / json.loads((root / "manifest.json").read_text())["blocks"][0]
    return ScenarioConfig.model_validate(
        yaml.safe_load((block / "scenario.yaml").read_text())
    ), EnvironmentConfig.model_validate(
        yaml.safe_load((block / "environment.yaml").read_text())
    )


@pytest.mark.nanofaas
def test_plan_is_lazy_and_rejects_reuse(tmp_path, nanofaas_root):
    from nanolab.plans.module_cost import build_module_cost_plan

    config, environment = prepared(tmp_path)
    executor = RecordingExecutor()
    directory = tmp_path / "run"

    def plan():
        return build_module_cost_plan(
            config,
            environment,
            RoleBindings({"host": executor, "stack": executor, "loadgen": executor}),
            control_plane_url="http://stack:30080",
            prometheus_client=NoopPrometheus(),
            run_dir=directory,
            repo_root=nanofaas_root,
        )

    tasks = plan().compile().tasks
    assert (
        len([t for t in tasks if t.task.title.startswith("Measure module cost:")]) == 8
    )
    assert not directory.exists()
    directory.mkdir()
    (directory / "protocol.json").write_text("{}")
    with pytest.raises(ValueError, match="fresh"):
        plan()


@pytest.mark.parametrize("bad", ["regular", "shared", "external"])
def test_preflight_requires_owned_spot_pair(tmp_path, bad):
    from nanolab.plans.module_cost import require_module_environment

    _, environment = prepared(tmp_path)
    if bad == "regular":
        environment.azure.priority = "Regular"
    elif bad == "shared":
        environment.roles["loadgen"].name = environment.roles["stack"].name
    else:
        environment.roles["stack"].host = "existing.example"
    with pytest.raises(ValueError, match=r"Spot|distinct|created"):
        require_module_environment(environment)


@pytest.mark.nanofaas
@pytest.mark.parametrize("failure", [False, True])
def test_real_platform_sequence_uses_one_function_digest_and_cleans_before_next(
    tmp_path, nanofaas_root, monkeypatch, failure
):
    from types import SimpleNamespace

    from sonata_engine import TaskOutcome

    from nanolab.plans.module_cost import build_module_cost_plan
    from nanolab.tasks import module_cost_report
    from nanolab.tasks.module_cost import MeasureModuleArm
    from nanolab.tasks.module_cost_build import BuildModuleCost

    config, environment = prepared(tmp_path)
    executor = RecordingExecutor()
    directory = tmp_path / "results"

    def build(self, _inputs):
        assert not executor.seen
        for arm in config.module_cost.order:
            dest = directory / "build" / arm
            dest.mkdir(parents=True)
            (dest / "images.json").write_text(
                json.dumps(
                    {
                        "function": {"digest": "127.0.0.1:5000/fn@sha256:" + "a" * 64},
                        "control-plane": {
                            "digest": "cp@sha256:" + "b" * 64,
                            "reference": f"cp/{arm}:block",
                        },
                    }
                )
            )
        (directory / "protocol.json").write_text("{}")
        return TaskOutcome(
            value={
                arm: SimpleNamespace(
                    control_plane=lambda arm=arm: SimpleNamespace(
                        mode=arm.split("-")[0],
                        image=SimpleNamespace(reference=f"cp/{arm}:block"),
                    ),
                    modules=(),
                )
                for arm in config.module_cost.order
            }
        )

    measured = []

    def measure(self, _inputs):
        measured.append(self.directory.name)
        if failure:
            raise RuntimeError("measurement broke")
        return TaskOutcome(value=None)

    monkeypatch.setattr(BuildModuleCost, "run", build)
    monkeypatch.setattr(MeasureModuleArm, "run", measure)
    monkeypatch.setattr(module_cost_report, "analyze_arm", lambda *_: {})
    monkeypatch.setattr(
        module_cost_report, "analyze_module_cost", lambda *_: {"comparable": True}
    )
    plan = build_module_cost_plan(
        config,
        environment,
        RoleBindings({"host": executor, "stack": executor, "loadgen": executor}),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=directory,
        repo_root=nanofaas_root,
    )
    if failure:
        with pytest.raises(Exception, match="measurement broke"):
            plan.run()
    else:
        plan.run()
        assert measured == config.module_cost.order
    commands = [" ".join(s.argv) for s in executor.seen]
    assert not any("./gradlew" in c or "docker build" in c for c in commands)
    installs = [i for i, c in enumerate(commands) if "helm upgrade" in c]
    deletes = [i for i, c in enumerate(commands) if "helm uninstall" in c]
    assert len(deletes) == (1 if failure else 8)
    assert all(a < b for a, b in zip(deletes, installs[1:], strict=False))
    bodies = [
        json.loads((directory / "arms" / arm / "registration.json").read_text())
        for arm in measured
    ]
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["maxRetries"] == 0
    assert bodies[0]["scalingConfig"]["concurrencyControl"]["mode"] == "FIXED"
