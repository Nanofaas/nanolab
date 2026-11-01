import subprocess
from types import SimpleNamespace

import pytest
from sonata_tasks.tasks.models import TaskResult


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
    stage_bytes(SimpleNamespace(run=run), "loadgen", tmp_path / "input.json", content)
    assert (tmp_path / "input.json").read_bytes() == content
    assert len(calls) > 1
    with pytest.raises(RuntimeError, match="exists"):
        stage_bytes(
            SimpleNamespace(run=run), "loadgen", tmp_path / "input.json", content
        )


def test_native_recipe_does_not_replace_explicit_cpu_limit(tmp_path):
    from sonata_engine import Resource, TaskInputs

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

    value = SimpleNamespace(
        control_plane=lambda: SimpleNamespace(
            mode="native", image=SimpleNamespace(reference="repo/cp:tag")
        )
    )
    distribution = Resource(
        title="test", acquire=lambda _: value, release=lambda *_: None
    )
    request = PlatformRequest(
        backend="k8s",
        functions=(PlatformFunction("fn", "image", "{}", ("true",)),),
        recipe=RecipeBinding(distribution, {}, tmp_path),
        helm_values=("--set", "controlPlane.resources.limits.cpu=2"),
    )
    executor = SimpleNamespace(run=run, binding_key=lambda role: role)
    release = _helm_release_with_endpoint(request, executor, tmp_path, ())
    release.acquire(TaskInputs._for_resources({distribution: value}, {distribution}))
    command = next(c for c in commands if "helm" in c.argv)
    assert "controlPlane.resources.limits.cpu=2" in command.argv
    assert "controlPlane.resources.limits.cpu=1" not in command.argv
