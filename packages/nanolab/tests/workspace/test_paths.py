import os
from pathlib import Path

import pytest

from nanolab.workspace import paths as workspace_paths
from nanolab.workspace.paths import ToolPaths, default_tool_paths


def test_tool_paths_preserve_separate_source_and_tool_roots() -> None:
    paths = ToolPaths.from_roots(
        Path("/nanofaas"), Path("/nanolab"), workspace_root=Path("/operator")
    )

    assert paths.nanofaas_root == Path("/nanofaas")
    assert paths.tool_root == Path("/nanolab")
    assert paths.profiles_dir == Path("/operator/profiles")
    assert paths.runs_dir == Path("/operator/runs")
    assert paths.scenarios_dir == Path("/nanolab/scenarios")
    assert paths.scenario_payloads_dir == Path("/nanolab/scenarios/payloads")


def test_default_tool_paths_requires_nanofaas_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NANOFAAS_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="NANOFAAS_ROOT"):
        default_tool_paths()


def test_default_tool_paths_accepts_valid_nanofaas_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nanofaas_root = tmp_path / "nanofaas"
    nanofaas_root.mkdir()
    (nanofaas_root / "build.gradle").touch()
    (nanofaas_root / "settings.gradle").touch()
    monkeypatch.setenv("NANOFAAS_ROOT", os.fspath(nanofaas_root))

    paths = default_tool_paths()

    assert paths.nanofaas_root == nanofaas_root.resolve()
    assert paths.tool_root == workspace_paths.bundled_assets_root() / "presets"


def test_default_tool_paths_rejects_invalid_nanofaas_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "not-nanofaas"
    checkout.mkdir()
    monkeypatch.setenv("NANOFAAS_ROOT", os.fspath(checkout))

    with pytest.raises(RuntimeError, match=r"build.gradle, settings.gradle"):
        default_tool_paths()


def test_default_outputs_follow_operator_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "source"
    checkout.mkdir()
    (checkout / "build.gradle").touch()
    (checkout / "settings.gradle").touch()
    monkeypatch.setenv("NANOFAAS_ROOT", str(checkout))
    workspace = tmp_path / "operator"
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(workspace))
    paths = default_tool_paths()
    assert paths.runs_dir == workspace / "runs"
    assert paths.profiles_dir == workspace / "profiles"
    assert paths.tool_root != workspace


def test_distributed_resources_have_one_root_in_source_and_wheel() -> None:
    assert workspace_paths.discover_tool_root() == (
        workspace_paths.bundled_assets_root() / "presets"
    )


def test_named_input_precedence_preserves_operator_files(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    workspace = tmp_path / "operator"
    (workspace / "scenarios").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(workspace))
    name = Path("deployment-lifecycle-container.yaml")
    bundled = workspace_paths.bundled_assets_root() / "presets/scenarios" / name
    assert workspace_paths.resolve_input_path(name, "scenarios") == bundled
    custom = workspace / "scenarios" / name
    custom.write_text("operator input")
    assert workspace_paths.resolve_input_path(name, "scenarios") == custom
    local = cwd / name
    local.write_text("explicit input")
    assert workspace_paths.resolve_input_path(name, "scenarios") == local
    assert workspace_paths.resolve_input_path(custom, "scenarios") == custom
    with pytest.raises(ValueError, match="does not exist"):
        workspace_paths.resolve_input_path(Path("missing") / name, "scenarios")


def test_inspect_named_preset_outside_checkout_without_source(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from nanolab.app.main import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NANOFAAS_ROOT", raising=False)
    monkeypatch.delenv("NANOLAB_WORKSPACE", raising=False)
    result = CliRunner().invoke(app, ["inspect", "deployment-lifecycle-container.yaml"])
    assert result.exit_code == 0, (result.output, result.exception)
    recipe = Path(json.loads(result.output)["recipeProfile"])
    assert recipe.is_file()
    assert recipe.is_relative_to(workspace_paths.bundled_assets_root())


def test_missing_nested_preset_is_a_cli_parameter_error(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from nanolab.app.main import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app, ["inspect", "missing/deployment-lifecycle-container.yaml"]
    )
    assert result.exit_code == 2
    assert "does not exist" in result.output
