from __future__ import annotations

import os
from pathlib import Path

from sonata_tasks.execution.bindings import RoleBindings

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.validate import build_validate_plan
from tests.plans.test_validate import RecordingExecutor


def test_recipe_plan_schedules_publish_without_legacy_builds(tmp_path: Path) -> None:
    profile = (
        Path(__file__).resolve().parents[2] / "recipes/validate-container-jvm.yaml"
    )
    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": "container",
            "functions": ["word-stats-java"],
            "recipeProfile": str(profile),
        }
    )
    executor = RecordingExecutor()
    run_dir = tmp_path / "run"
    workflow = build_validate_plan(
        config,
        RoleBindings({"host": executor}),
        repo_root=Path(os.environ["NANOFAAS_ROOT"]),
        tool_root=Path(__file__).resolve().parents[2],
        run_dir=run_dir,
    )
    titles = [task.task.title for task in workflow.compile().tasks]
    assert any("Publish recipe" in title for title in titles)
    assert not any("Build image word-stats-java" in title for title in titles)
    assert not run_dir.exists()
    assert not executor.seen
