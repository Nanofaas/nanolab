from __future__ import annotations

import os
from pathlib import Path

import pytest
from sonata_tasks.execution.bindings import RoleBindings

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.validate import build_validate_plan, require_recipe_environment
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


def test_one_kubernetes_recipe_scenario_uses_provider_specific_delivery(
    tmp_path: Path,
) -> None:
    profile = Path(__file__).resolve().parents[2] / "recipes/validate-k8s-jvm.yaml"
    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": "k8s",
            "functions": ["word-stats-java"],
            "recipeProfile": str(profile),
        }
    )
    executor = RecordingExecutor()
    bindings = RoleBindings({"host": executor, "stack": executor})
    titles_by_provider: dict[str, list[str]] = {}
    for provider in ("local", "multipass"):
        run_dir = tmp_path / provider
        environment = EnvironmentConfig.model_validate(
            {"provider": provider, "roles": {"stack": {"name": "test-stack"}}}
            if provider == "multipass"
            else {"provider": "local"}
        )
        workflow = build_validate_plan(
            config,
            bindings,
            repo_root=Path(os.environ["NANOFAAS_ROOT"]),
            tool_root=Path(__file__).resolve().parents[2],
            environment=environment,
            run_dir=run_dir,
        )
        titles_by_provider[provider] = [
            step.task.title for step in workflow.compile().tasks
        ]
        assert not run_dir.exists()
    local = titles_by_provider["local"]
    multipass = titles_by_provider["multipass"]
    assert any("Assemble staged recipe" in title for title in local)
    assert any("Load recipe images" in title for title in local)
    assert any("Forward recipe API" in title for title in local)
    assert not any("Publish staged recipe" in title for title in local)
    assert any("Publish staged recipe" in title for title in multipass)
    assert not any("Load recipe images" in title for title in multipass)
    assert local.count("Build staged Kubernetes queue probe") == 1
    assert not executor.seen


def test_unsupported_recipe_provider_fails_before_provisioning(tmp_path: Path) -> None:
    profile = Path(__file__).resolve().parents[2] / "recipes/validate-k8s-jvm.yaml"
    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": "k8s",
            "functions": ["word-stats-java"],
            "recipeProfile": str(profile),
        }
    )
    external = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {"stack": {"host": "vm.example", "user": "ubuntu"}},
        }
    )
    with pytest.raises(ValueError, match="provider external"):
        require_recipe_environment(config, external)


def test_container_recipe_services_join_existing_validation_cycle(
    tmp_path: Path,
) -> None:
    import yaml

    base = Path(__file__).resolve().parents[2] / "recipes/validate-container-jvm.yaml"
    profile = tmp_path / "services.yaml"
    data = yaml.safe_load(base.read_text())
    data["services"] = [
        {
            "name": "warm-echo",
            "sdk": "java",
            "build": {"mode": "jvm"},
            "container": {"image": "warm-echo"},
        }
    ]
    profile.write_text(yaml.safe_dump(data))
    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": "container",
            "functions": ["word-stats-java"],
            "recipeProfile": str(profile),
        }
    )
    executor = RecordingExecutor()
    workflow = build_validate_plan(
        config,
        RoleBindings({"host": executor}),
        repo_root=Path(os.environ["NANOFAAS_ROOT"]),
        tool_root=Path(__file__).resolve().parents[2],
        run_dir=tmp_path / "run",
    )
    titles = [step.task.title for step in workflow.compile().tasks]
    assert "Invoke warm-echo" in titles
    assert "Verify recipe image of warm-echo" in titles
    assert "Verify warm-echo HTTP envelope" in titles
    assert any("Release warm-echo" in title for title in titles)
    assert not any("Build image" in title for title in titles)
    assert not executor.seen


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([{"name": "watchdog", "sdk": "dockerfile"}], "Java invocation"),
        ([{"name": "missing", "sdk": "java"}], "lacks function.yaml"),
        ([{"name": "../warm-echo", "sdk": "java"}], "simple component name"),
        ([{"name": "warm-echo", "sdk": "java"}] * 2, "unique registration"),
    ],
)
def test_invalid_recipe_service_fails_before_provisioning(
    tmp_path: Path, entries: list[dict[str, str]], message: str
) -> None:
    import yaml

    from nanolab.plans.functions import resolve_recipe_services

    profile = tmp_path / "recipe.yaml"
    profile.write_text(yaml.safe_dump({"services": entries}))
    with pytest.raises(ValueError, match=message):
        resolve_recipe_services(
            profile,
            source_root=Path(os.environ["NANOFAAS_ROOT"]),
            tool_root=Path(__file__).resolve().parents[2],
        )
