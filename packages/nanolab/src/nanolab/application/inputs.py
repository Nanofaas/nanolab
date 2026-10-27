"""Operator inputs: scenario and environment files, read into their configs.

The CLI and the TUI load inputs the same way, so the rules live here: preset
names resolve through the operator workspace and the distributed presets, and
the files a scenario names resolve beside it.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import yaml

from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.tasks.soak.artifacts import fingerprint
from nanolab.workspace.paths import resolve_input_path


def read_mapping(path: Path) -> dict[str, object]:
    """Read a YAML file that must hold a mapping."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration must be an object: {path}")
    return data


def load_scenario(path: Path) -> ScenarioConfig:
    """Load a scenario, resolving the files it names beside it.

    `path` is a preset name or an operator path. A recipe profile, a soak
    policy and the one-shot artifacts are relative to the scenario's directory.
    """
    path = resolve_input_path(path, "scenarios")
    data = read_mapping(path)
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
    config = ScenarioConfig.model_validate(data)
    if config.one_shot is not None:
        for artifact in (
            config.one_shot.profile,
            config.one_shot.qualification,
            config.one_shot.runtime_distribution,
            config.one_shot.observability.vm_resource_overhead,
        ):
            if artifact is not None:
                artifact.path = (path.resolve().parent / artifact.path).resolve()
    return config


def load_environment(path: Path | None) -> EnvironmentConfig:
    """Load an environment by preset name or path; none means local."""
    return (
        EnvironmentConfig.model_validate(
            read_mapping(resolve_input_path(path, "environments"))
        )
        if path
        else EnvironmentConfig(provider="local")
    )


def load_soak_policy(
    data: dict[str, object], scenario_path: Path
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Resolve an external criteria policy and a receipt identifying it.

    The document carries criteria only: it cannot change the runtime, images
    or timing the run measures.
    """
    resolved = copy.deepcopy(data)
    if "soakPolicyFile" not in resolved:
        return resolved, None
    if resolved.get("workflow") != "soak":
        raise ValueError("soakPolicyFile is only supported for workflow soak")
    reference = resolved.pop("soakPolicyFile")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("soak policy file must be an explicit path")
    path = Path(reference)
    if not path.is_absolute():
        path = scenario_path.parent / path
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
    except OSError as error:
        raise ValueError(
            f"required soak policy file unavailable: {path}: {error}"
        ) from error
    if len(raw) > 1024 * 1024:
        raise ValueError("soak policy file exceeds size limit")
    policy = yaml.safe_load(raw)
    if (
        not isinstance(policy, dict)
        or set(policy) != {"schema", "criteria"}
        or policy["schema"] != "nanolab-soak-policy-v1"
        or not isinstance(policy["criteria"], list)
        or not policy["criteria"]
    ):
        raise ValueError(
            "soak policy must contain schema nanolab-soak-policy-v1 and nonempty "
            "criteria only"
        )
    soak = resolved.get("soak")
    if not isinstance(soak, dict):
        raise ValueError("soak policy requires a soak configuration block")
    soak["criteria"] = policy["criteria"]
    receipt = {
        "path": str(path.resolve()),
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "resolved_criteria_fingerprint": fingerprint({"criteria": policy["criteria"]}),
    }
    return resolved, receipt
