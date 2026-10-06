"""Resolved locations: the nanoFaaS checkout, this tool's outputs, its assets."""

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
    def from_roots(
        cls, nanofaas_root: Path, tool_root: Path, *, workspace_root: Path | None = None
    ) -> ToolPaths:
        """Resolve source/resources separately from writable operator directories."""
        source_root = Path(nanofaas_root)
        product_root = Path(tool_root)
        workspace = (
            workspace_root if workspace_root is not None else operator_workspace_root()
        )
        return cls(
            nanofaas_root=source_root,
            tool_root=product_root,
            profiles_dir=workspace / "profiles",
            runs_dir=workspace / "runs",
            scenarios_dir=product_root / "scenarios",
            scenario_payloads_dir=product_root / "scenarios" / "payloads",
        )


def discover_tool_root() -> Path:
    """Return the canonical distributed presets in source and installations."""
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


def resolve_input_path(path: Path, directory: str) -> Path:
    """Resolve an explicit input or a named workspace/distributed preset."""
    path = path.expanduser()
    candidates = [path]
    if not path.is_absolute() and len(path.parts) == 1:
        candidates.extend(
            (
                operator_workspace_root() / directory / path,
                discover_tool_root() / directory / path,
            )
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError(f"configuration file does not exist: {path}")


def default_tool_paths() -> ToolPaths:
    """Return the tool paths for the checkout named by ``NANOFAAS_ROOT``."""
    return ToolPaths.from_roots(nanofaas_root_from_env(), discover_tool_root())
