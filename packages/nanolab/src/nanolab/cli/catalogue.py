"""Read scenario/environment configuration and describe the public catalogue."""

from pathlib import Path

import typer
import yaml

from nanolab.cli.soak import load_soak_policy
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.workspace.paths import resolve_input_path

_ENVIRONMENT_PROVIDERS = ("local", "multipass", "external", "azure", "proxmox")


def scenario_input(value: Path) -> Path:
    """Resolve a CLI scenario path or preset name with a parameter diagnostic."""
    return _input(value, "scenarios")


def environment_input(value: Path | None) -> Path | None:
    """Resolve an optional CLI environment path or preset name."""
    return _input(value, "environments") if value is not None else None


def _input(value: Path, directory: str) -> Path:
    try:
        return resolve_input_path(value, directory)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from None


def _read(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration must be an object: {path}")
    return data


def _scenario(path: Path) -> ScenarioConfig:
    path = resolve_input_path(path, "scenarios")
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
        EnvironmentConfig.model_validate(
            _read(resolve_input_path(path, "environments"))
        )
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
