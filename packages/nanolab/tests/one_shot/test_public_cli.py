from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from sonata_engine import Workflow

from nanolab.cli.catalogue import _scenario
from nanolab.cli.product import _workflow


@pytest.mark.parametrize("name", ["calibration", "qualification", "experiment"])
def test_cli_routes_one_shot_to_dedicated_builder_with_run_directory(
    tmp_path, monkeypatch, name
):
    calls = []

    def build(config, environment, **kwargs):
        calls.append(kwargs)
        return Workflow("one-shot-" + name)

    module = "nanolab.plans.one_shot_" + name
    monkeypatch.setattr(module + ".build_one_shot_" + name + "_plan", build)
    monkeypatch.setattr(
        "nanolab.cli.product.default_tool_paths",
        lambda: SimpleNamespace(nanofaas_root=tmp_path, runs_dir=tmp_path / "runs"),
    )
    workflow = _workflow(
        cast(Any, SimpleNamespace(workflow="one-shot-" + name)),
        cast(Any, object()),
        run_dir=tmp_path / "run",
    )
    assert isinstance(workflow, Workflow)
    assert calls == [{"run_dir": tmp_path / "run", "repo_root": tmp_path}]


def test_relative_prerequisite_paths_resolve_from_scenario_not_operator_directory(
    tmp_path, monkeypatch
):
    preset = (
        Path(__file__).parents[2]
        / "src/nanolab/assets/presets/scenarios/one-shot-experiment.yaml"
    )
    data = yaml.safe_load(preset.read_text())
    data["oneShot"]["runtimeDistribution"] = {
        "path": "distribution.json",
        "sha256": "a" * 64,
    }
    directory = tmp_path / "scenarios"
    directory.mkdir()
    path = directory / "comparison.yaml"
    path.write_text(yaml.safe_dump(data))
    monkeypatch.chdir(tmp_path)
    config = _scenario(path)
    assert config.one_shot is not None
    for name in ["profile", "qualification", "runtime_distribution"]:
        artifact = getattr(config.one_shot, name)
        assert artifact.path.is_absolute() and artifact.path.parent == directory


def test_public_cli_does_not_provision_legacy_stack_before_one_shot_preflight(
    monkeypatch,
):
    from nanolab.cli.product import _provisioning_context

    def forbidden(*args, **kwargs):
        pytest.fail("one-shot must acquire only its run-owned topology after preflight")

    monkeypatch.setattr("nanolab.cli.product.provision_environment", forbidden)
    context = _provisioning_context(
        cast(Any, SimpleNamespace(workflow="one-shot-experiment", backend="container")),
        cast(Any, SimpleNamespace(provider="multipass")),
        cast(Any, SimpleNamespace(nanofaas_root=Path("/unused"))),
        False,
    )
    with context:
        pass


@pytest.mark.parametrize("report_fails", [False, True])
def test_public_cli_load_failure_still_reports_and_preserves_original_error(
    tmp_path, monkeypatch, capsys, report_fails
):
    from unittest.mock import MagicMock

    import nanolab.cli.product as product

    preset = (
        Path(__file__).parents[2]
        / "src/nanolab/assets/presets/scenarios/one-shot-experiment.yaml"
    )
    scenario = _scenario(preset)
    calls = []
    monkeypatch.setattr(product, "require_supported_runtimes", lambda *args: None)
    monkeypatch.setattr(product, "require_recipe_environment", lambda *args: None)
    monkeypatch.setattr(
        product, "_build_run_workflow", lambda **kwargs: SimpleNamespace(keep=False)
    )
    monkeypatch.setattr(product, "_workflow_observers", lambda *args: ())

    def fail_load(*args, **kwargs):
        raise RuntimeError("load aborted")

    def report(path):
        calls.append(path)
        if report_fails:
            raise ValueError("invalid report data")

    monkeypatch.setattr(product, "_run_selected_workflow", fail_load)
    monkeypatch.setattr("nanolab.one_shot.report.render_campaign", report)
    with pytest.raises(RuntimeError, match="load aborted"):
        product._execute_workflow(
            sink=MagicMock(),
            scenario_config=scenario,
            environment_config=cast(Any, object()),
            paths=cast(Any, SimpleNamespace(nanofaas_root=tmp_path)),
            keep=False,
            control_plane_url=None,
            prometheus_url=None,
            effective_run_dir=tmp_path / "run",
            only=None,
            start=None,
            until=None,
            scenario=preset,
            release_request=None,
            release_provider=None,
            release_journal=None,
            resume=False,
            provision=False,
        )
    assert calls == [tmp_path / "run"]
    if report_fails:
        assert "one-shot report unavailable" in capsys.readouterr().err


def test_catalogue_classifies_one_shot_as_multipass_not_local_container(tmp_path):
    from nanolab.cli.catalogue import _workflow_catalog

    (tmp_path / "one-shot.yaml").write_text(
        "workflow: one-shot-experiment\nbackend: container\n"
    )
    assert _workflow_catalog(tmp_path)["one-shot-experiment"][0] == ("multipass",)
