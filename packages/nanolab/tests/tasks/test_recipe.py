from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from nanolab.tasks.recipe import (
    AssembleRecipeTask,
    PublishRecipeTask,
    read_distribution,
)
from nanolab.workspace.recipe import RecipeRun


def _report(recipe: Path, tag: str, status: str) -> dict[str, Any]:
    image = {
        "reference": f"127.0.0.1:5000/nanofaas/control-plane:{tag}",
        "id": "sha256:abc",
        "status": status,
    }
    if status == "published":
        image["digest"] = "sha256:def"
    return {
        "schemaVersion": 2,
        "recipe": {
            "name": "test",
            "sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
            "schemaVersion": 2,
        },
        "tag": tag,
        "source": {"revision": "123", "dirty": True},
        "modules": ["build-metadata", "container-deployment-provider"],
        "components": [
            {
                "kind": "control-plane",
                "name": "control-plane",
                "sdk": "java",
                "mode": "jvm",
                "variant": "recipe-v2-jvm",
                "optimization": "c2",
                "image": image,
            }
        ],
    }


class _Executor:
    def __init__(
        self, report: Path, data: dict[str, Any], *, fail: bool = False
    ) -> None:
        self.report = report
        self.data = data
        self.fail = fail
        self.specs: list[CommandTaskSpec] = []

    def binding_key(self, role: str) -> str:
        return role

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.specs.append(task)
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report.write_text(json.dumps(self.data))
        return TaskResult(
            task_id="",
            status="failed" if self.fail else "passed",
            return_code=1 if self.fail else 0,
        )


@pytest.mark.parametrize(
    ("task_type", "target", "status"),
    [
        (AssembleRecipeTask, "assembleRecipe", "built"),
        (PublishRecipeTask, "publishRecipe", "published"),
    ],
)
def test_recipe_task_runs_one_gradle_target(
    tmp_path: Path, task_type, target: str, status: str
) -> None:
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("schemaVersion: 2\n")
    run = RecipeRun(tmp_path / "source", recipe, tmp_path / "output", "run-1")
    executor = _Executor(
        run.output_dir / "distribution.json", _report(recipe, run.tag, status)
    )

    distribution = task_type(run, executor=executor).run(TaskInputs.empty()).value

    assert distribution is not None
    assert distribution.control_plane().image.id == "sha256:abc"
    assert (run.output_dir.parent / "gradle.log").is_file()
    assert len(executor.specs) == 1
    assert executor.specs[0].argv[1] == target
    assert executor.specs[0].options.cwd == run.source_dir
    assert f"-Precipe={recipe}" in executor.specs[0].argv
    assert f"-PrecipeTag={run.tag}" in executor.specs[0].argv
    assert f"-PrecipeOutput={run.output_dir}" in executor.specs[0].argv


@pytest.mark.parametrize(
    ("published", "status", "digest"),
    [
        (False, "published", "sha256:def"),
        (True, "built", None),
        (True, "published-unverified", None),
        (True, "failed", None),
    ],
)
def test_report_rejects_wrong_image_status(
    tmp_path: Path, published: bool, status: str, digest: str | None
) -> None:
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("schemaVersion: 2\n")
    data = _report(recipe, "run-1", status)
    image = data["components"][0]["image"]
    if digest is None:
        image.pop("digest", None)
    report = tmp_path / "distribution.json"
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="status"):
        read_distribution(report, recipe=recipe, tag="run-1", published=published)


def test_failed_command_cannot_return_stale_report(tmp_path: Path) -> None:
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("schemaVersion: 2\n")
    run = RecipeRun(tmp_path / "source", recipe, tmp_path / "output", "run-1")
    executor = _Executor(
        run.output_dir / "distribution.json",
        _report(recipe, run.tag, "published"),
        fail=True,
    )
    with pytest.raises(RuntimeError, match="failed"):
        PublishRecipeTask(run, executor=executor).run(TaskInputs.empty())
