"""Exercise the wheel from an operator workspace outside its source checkout."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_installed_wheel_catalogue_presets_and_writable_workspace(
    tmp_path: Path,
) -> None:
    package = Path(__file__).resolve().parents[1]
    build_source = tmp_path / "build-source"
    build_source.mkdir()
    for name in ("pyproject.toml", "LICENSE", "README.md"):
        shutil.copyfile(package / name, build_source / name)
    shutil.copytree(
        package / "src",
        build_source / "src",
        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"),
    )
    built = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--offline",
            str(build_source),
            "--out-dir",
            str(tmp_path / "dist"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    installed = tmp_path / "installed"
    installed_result = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            str(installed),
            str(next((tmp_path / "dist").glob("*.whl"))),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed_result.returncode == 0, installed_result.stderr
    operator = tmp_path / "operator"
    operator.mkdir()
    env = {**os.environ, "PYTHONPATH": str(installed)}
    env.pop("NANOLAB_WORKSPACE", None)
    checked = subprocess.run(
        [
            sys.executable,
            str(package.parents[1] / "scripts/smoke-installed-wheel.py"),
            str(installed),
        ],
        cwd=operator,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_bundled_scenario_recipe_and_policy_references_resolve() -> None:
    import yaml

    from nanolab.cli.catalogue import _scenario
    from nanolab.workspace.paths import discover_tool_root

    scenarios = discover_tool_root() / "scenarios"
    checked = []
    for path in scenarios.glob("*.yaml"):
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict) or "workflow" not in data:
            continue
        config = _scenario(path)
        if config.recipe_profile is not None:
            assert config.recipe_profile.is_file(), path
        if "soakPolicyFile" in data:
            assert (path.parent / data["soakPolicyFile"]).is_file(), path
        checked.append(path)
    assert checked
