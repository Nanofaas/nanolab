"""Resolved locations for the nanoFaaS checkout and this tool's own outputs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_NANOFAAS_MARKERS = ("build.gradle", "settings.gradle")


@dataclass(frozen=True)
class ToolPaths:
    """The roots and output directories a run resolves once and reuses."""

    nanofaas_root: Path
    tool_root: Path
    profiles_dir: Path
    runs_dir: Path
    scenarios_dir: Path
    scenario_payloads_dir: Path

    @classmethod
    def from_roots(cls, nanofaas_root: Path, tool_root: Path) -> ToolPaths:
        """Build a path set from an explicit checkout root and tool root."""
        source_root = Path(nanofaas_root)
        product_root = Path(tool_root)
        return cls(
            nanofaas_root=source_root,
            tool_root=product_root,
            profiles_dir=product_root / "profiles",
            runs_dir=product_root / "runs",
            scenarios_dir=product_root / "scenarios",
            scenario_payloads_dir=product_root / "scenarios" / "payloads",
        )


def discover_tool_root() -> Path:
    """Return the root of the ``nanolab`` package this module ships in."""
    return Path(__file__).resolve().parents[3]


def nanofaas_root_from_env() -> Path:
    """Return the nanoFaaS checkout named by ``NANOFAAS_ROOT``.

    Raises RuntimeError when the variable is unset or empty, or when the path
    does not look like a checkout because a Gradle marker file is missing.
    """
    value = os.getenv("NANOFAAS_ROOT", "").strip()
    if not value:
        raise RuntimeError("NANOFAAS_ROOT must point to a nanoFaaS checkout")
    root = Path(value).expanduser().resolve()
    missing = [marker for marker in _NANOFAAS_MARKERS if not (root / marker).is_file()]
    if missing:
        raise RuntimeError(
            f"NANOFAAS_ROOT is not a nanoFaaS checkout; missing: {', '.join(missing)}"
        )
    return root


def default_tool_paths() -> ToolPaths:
    """Return the tool paths for the checkout named by ``NANOFAAS_ROOT``."""
    return ToolPaths.from_roots(nanofaas_root_from_env(), discover_tool_root())


def scenario_path_from_env(cli_path: Path | None = None) -> Path | None:
    """Return the scenario path to use, preferring an explicit CLI argument.

    Falls back to ``NANOFAAS_SCENARIO_PATH``; None when neither is supplied.
    """
    if cli_path is not None:
        return cli_path

    s = os.getenv("NANOFAAS_SCENARIO_PATH", "").strip()
    return Path(s) if s else None
