"""Checks a scenario's run options pass before anything is provisioned.

They raise `ValueError`: the CLI reports it as a bad parameter, the TUI as a
configuration error.
"""

from __future__ import annotations

from pathlib import Path

from nanolab.config import EnvironmentConfig, ScenarioConfig


def require_cli_endpoint(
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    control_plane_url: str | None,
) -> None:
    """Require an endpoint for a k8s cli scenario that provisions nothing."""
    if (
        scenario.workflow == "cli"
        and scenario.backend == "k8s"
        and scenario.recipe_profile is None
        and environment.provider == "local"
        and control_plane_url is None
    ):
        raise ValueError("--control-plane-url is required for a k8s cli scenario")


def validate_contract_options(
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    keep: bool = False,
    resume: bool = False,
    teardown: bool = False,
    only: str | None = None,
    start: str | None = None,
    until: str | None = None,
    control_plane_url: str | None = None,
    prometheus_url: str | None = None,
    release_config: Path | None = None,
) -> None:
    """Refuse the options a contract run, which owns its whole lifecycle, ignores."""
    if scenario.workflow != "contract":
        return
    if environment.provider != "local":
        raise ValueError("contract requires a local environment")
    if (
        keep
        or resume
        or teardown
        or any(
            value is not None
            for value in (
                only,
                start,
                until,
                control_plane_url,
                prometheus_url,
                release_config,
            )
        )
    ):
        raise ValueError(
            "contract requires its complete owned lifecycle; keep, resume, teardown, "
            "partial selection, external endpoints and release settings are unsupported"
        )


def validate_cli_container_options(
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    keep: bool = False,
) -> None:
    """Refuse a cli container scenario outside a local environment, or kept."""
    if scenario.workflow != "cli" or scenario.backend != "container":
        return
    if environment.provider != "local":
        raise ValueError("cli container scenario requires a local environment")
    if keep:
        raise ValueError("--keep is not supported for a cli container scenario")
