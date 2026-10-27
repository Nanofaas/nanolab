from __future__ import annotations

import tomllib
from pathlib import Path


def test_nanolab_has_no_root_runtime_modules() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "nanolab"

    root_modules = sorted(path.name for path in package_root.glob("*.py"))

    assert root_modules == ["__init__.py"]


def test_remaining_root_modules_are_importable_from_semantic_packages() -> None:
    from nanolab.plans.validate import build_validate_plan  # noqa: F401


def test_app_package_contains_only_entrypoint_modules() -> None:
    app_root = Path(__file__).resolve().parents[1] / "src" / "nanolab" / "app"

    app_modules = sorted(path.name for path in app_root.glob("*.py"))

    assert app_modules == ["__init__.py", "main.py"]


def test_workspace_package_exposes_shared_tooling_primitives() -> None:
    from nanolab.workspace.paths import ToolPaths, default_tool_paths  # noqa: F401


def test_every_bundled_file_is_declared_package_data() -> None:
    # setuptools ships a data file only when a package-data glob names it, so a
    # file in a new asset directory is silently left out of the wheel. The
    # scripts under assets/ are data too: no package imports them.
    project = Path(__file__).resolve().parents[1]
    package_root = project / "src" / "nanolab"
    pyproject = tomllib.loads((project / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"]["nanolab"]
    declared = {path for pattern in patterns for path in package_root.glob(pattern)}
    bundled = {
        path
        for path in package_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.name != "ruff.toml"  # lint settings for the assets, not an asset
        and not any(part.startswith(".") for part in path.parts)
        and (path.suffix != ".py" or "assets" in path.relative_to(package_root).parts)
    }

    undeclared = sorted(
        str(path.relative_to(package_root)) for path in bundled - declared
    )

    assert undeclared == []


def test_one_shot_runtime_assets_and_environment_example_ship_inside_package():
    from importlib.resources import files

    assets = files("nanolab").joinpath("assets")
    for name in [
        "k6/one-shot.js",
        "one-shot/profile-set.schema.json",
        "presets/scenarios/one-shot-heterogeneous-calibration.yaml",
        "presets/scenarios/one-shot-heterogeneous-qualification.yaml",
        "presets/scenarios/one-shot-heterogeneous-pilot.yaml",
        "one-shot/contracts/service-profile.schema.json",
        "presets/environments/multipass-one-shot.yaml.example",
    ]:
        assert assets.joinpath(name).is_file()
