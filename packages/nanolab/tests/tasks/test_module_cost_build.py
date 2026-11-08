import subprocess
from types import SimpleNamespace
from typing import cast

import pytest
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.tasks.models import TaskResult

from nanolab.tasks.recipes.workflow import RecipeDistribution
from nanolab.tasks.resource_tasks import acquire_now, resource_of


def test_staging_chunks_roundtrip_and_reject_reuse(tmp_path):
    from nanolab.tasks.module_cost_build import stage_bytes

    calls = []

    def run(spec):
        calls.append(spec)
        assert sum(len(a) for a in spec.argv) < 50000
        result = subprocess.run(spec.argv, capture_output=True, text=True, check=False)
        return TaskResult(
            task_id=spec.task_id,
            status="passed",
            return_code=result.returncode,
            stderr=result.stderr,
        )

    content = b"actual corpus bytes\n" * 16000
    stage_bytes(
        cast(CommandTaskExecutor, SimpleNamespace(run=run)),
        "loadgen",
        tmp_path / "input.json",
        content,
    )
    assert (tmp_path / "input.json").read_bytes() == content
    assert len(calls) > 1
    with pytest.raises(RuntimeError, match="exists"):
        stage_bytes(
            cast(CommandTaskExecutor, SimpleNamespace(run=run)),
            "loadgen",
            tmp_path / "input.json",
            content,
        )


def test_native_recipe_does_not_replace_explicit_cpu_limit(tmp_path):
    from sonata_engine import TaskInputs

    from nanolab.tasks.platform import (
        PlatformFunction,
        PlatformRequest,
        _helm_release_with_endpoint,
    )
    from nanolab.tasks.recipes.workflow import RecipeBinding

    commands = []

    def run(spec, **kwargs):
        commands.append(spec)
        return TaskResult(
            task_id=spec.task_id, status="passed", return_code=0, stdout="10.0.0.1"
        )

    value = cast(
        RecipeDistribution,
        SimpleNamespace(
            control_plane=lambda: SimpleNamespace(
                mode="native", image=SimpleNamespace(reference="repo/cp:tag")
            )
        ),
    )
    distribution = resource_of("test", lambda _: value, lambda *_: None)
    request = PlatformRequest(
        backend="k8s",
        functions=(PlatformFunction("fn", "image", "{}", ("true",)),),
        recipe=RecipeBinding(distribution, {}, tmp_path),
        helm_values=("--set", "controlPlane.resources.limits.cpu=2"),
    )
    executor = cast(
        CommandTaskExecutor, SimpleNamespace(run=run, binding_key=lambda role: role)
    )
    release = _helm_release_with_endpoint(request, executor, tmp_path, ())
    acquire_now(
        release, TaskInputs._for_resources({distribution: value}, {distribution})
    )
    command = next(c for c in commands if "helm" in c.argv)
    assert "controlPlane.resources.limits.cpu=2" in command.argv
    assert "controlPlane.resources.limits.cpu=1" not in command.argv


def test_load_window_excludes_slow_staging_and_fetch(tmp_path, monkeypatch):
    from sonata_engine import TaskInputs, TaskOutcome

    import nanolab.tasks.module_cost as module
    import nanolab.tasks.module_cost_report as report
    from tests.plans.test_loadtest import NoopPrometheus

    current = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: current[0]))

    def command(_executor, _role, argv):
        if argv[0] == "mkdir":
            current[0] += 50
        return SimpleNamespace(stdout=str(current[0] + 20))

    def stage(*_):
        current[0] += 10

    class Runner:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, inputs):
            current[0] += 185
            return TaskOutcome(value=None)

    class Fetcher:
        def fetch_from(self, remote, local):
            current[0] += 100

    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module, "stage_bytes", stage)
    monkeypatch.setattr(module, "K6Task", Runner)
    monkeypatch.setattr(
        report,
        "read_load",
        lambda *args: {"first_send_ms": 95000, "last_terminal_ms": 275000},
    )
    (tmp_path / "inputs").mkdir()
    for name in ("performance-medium.json", "runtime-contract.json"):
        (tmp_path / "inputs" / name).write_text("{}")
    task = module.MeasureModuleArm(
        tmp_path,
        tmp_path,
        tmp_path,
        "http://unused",
        cast(CommandTaskExecutor, SimpleNamespace()),
        NoopPrometheus(),
        Fetcher(),
    )
    assert task.load("measurement", 180, TaskInputs.empty()) == (75, 255)
    assert current[0] == 455
