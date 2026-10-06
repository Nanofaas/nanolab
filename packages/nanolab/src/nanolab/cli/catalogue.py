"""Read scenario/environment configuration and describe the public catalogue."""

from pathlib import Path

import yaml

from nanolab.cli.soak import load_soak_policy
from nanolab.config import EnvironmentConfig, ScenarioConfig

_ENVIRONMENT_PROVIDERS = ("local", "multipass", "external", "azure", "proxmox")


def _read(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration must be an object: {path}")
    return data


def _scenario(path: Path) -> ScenarioConfig:
    data = _read(path)
    if "recipeProfile" in data:
        data["recipeProfile"] = (
            path.resolve().parent / str(data["recipeProfile"])
        ).resolve()
    if data.get("workflow") == "soak" or "soakPolicyFile" in data:
        resolved, receipt = load_soak_policy(data, path)
        config = ScenarioConfig.model_validate(resolved)
        if receipt is not None:
            object.__setattr__(config, "_soak_policy_receipt", receipt)
        return config
    return ScenarioConfig.model_validate(data)


def _environment(path: Path | None) -> EnvironmentConfig:
    return (
        EnvironmentConfig.model_validate(_read(path))
        if path
        else EnvironmentConfig(provider="local")
    )


def _workflow_catalog(
    scenarios_dir: Path,
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    workflows: dict[str, set[str]] = {}
    scenarios: dict[str, list[str]] = {}
    for path in scenarios_dir.glob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("workflow"), str):
            continue
        workflow = data["workflow"]
        environments = (
            ("local",) if data.get("backend") == "container" else _ENVIRONMENT_PROVIDERS
        )
        if workflow == "release":
            environments = ("azure",)
        workflows.setdefault(workflow, set()).update(environments)
        scenarios.setdefault(workflow, []).append(path.name)
    return {
        workflow: (
            tuple(
                provider
                for provider in _ENVIRONMENT_PROVIDERS
                if provider in environments
            ),
            tuple(sorted(scenarios[workflow])),
        )
        for workflow, environments in sorted(workflows.items())
    }
