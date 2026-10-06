"""Resolved locations: the nanoFaaS checkout, this tool's outputs, its assets."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
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
    """Return preset resources from a source checkout or the installed package."""
    checkout = Path(__file__).resolve().parents[3]
    source_module = checkout / "src/nanolab/workspace/paths.py"
    if source_module == Path(__file__).resolve():
        return checkout
    return bundled_assets_root() / "presets"


def bundled_assets_root() -> Path:
    """Return read-only runtime resources inside the installed package."""
    return Path(__file__).resolve().parents[1] / "assets"


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


def operator_workspace_root() -> Path:
    """Return writable operator inputs and outputs, independent of installation."""
    value = os.getenv("NANOLAB_WORKSPACE", "").strip()
    return Path(value).expanduser().resolve() if value else Path.cwd().resolve()


def default_tool_paths() -> ToolPaths:
    """Return the tool paths for the checkout named by ``NANOFAAS_ROOT``."""
    paths = ToolPaths.from_roots(nanofaas_root_from_env(), discover_tool_root())
    workspace = operator_workspace_root()
    return replace(
        paths, profiles_dir=workspace / "profiles", runs_dir=workspace / "runs"
    )
