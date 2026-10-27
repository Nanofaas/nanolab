from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from sonata_engine import Workflow

from nanolab.application.inputs import load_scenario
from nanolab.plans.dispatch import build_scenario_workflow

pytestmark = pytest.mark.usefixtures("simulated_one_shot_host")


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
        "nanolab.plans.dispatch.default_tool_paths",
        lambda: SimpleNamespace(nanofaas_root=tmp_path, runs_dir=tmp_path / "runs"),
    )
    workflow = build_scenario_workflow(
        cast(Any, SimpleNamespace(workflow="one-shot-" + name)),
        cast(Any, object()),
        run_dir=tmp_path / "run",
    )
    assert isinstance(workflow, Workflow)
    assert calls == [{"run_dir": tmp_path / "run", "repo_root": tmp_path}]


@pytest.mark.parametrize(
    "reference", ["relative", "absolute", "omitted", "missing", "bad-sha"]
)
def test_relative_prerequisite_paths_resolve_from_scenario_not_operator_directory(
    tmp_path, monkeypatch, reference
):
    import hashlib

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
    telemetry = directory / "overhead.json"
    telemetry.write_text('{"offline":"telemetry"}')
    content = telemetry.read_bytes()
    if reference != "omitted":
        data["oneShot"].setdefault("observability", {})["vmResourceOverhead"] = {
            "path": str(telemetry) if reference == "absolute" else "overhead.json",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        if reference == "missing":
            telemetry.unlink()
        elif reference == "bad-sha":
            telemetry.write_text("tampered")
    path = directory / "comparison.yaml"
    path.write_text(yaml.safe_dump(data))
    for cwd in (directory, tmp_path):
        monkeypatch.chdir(cwd)
        config = load_scenario(path)
        assert config.one_shot is not None
        for name in ["profile", "qualification", "runtime_distribution"]:
            artifact = getattr(config.one_shot, name)
            assert artifact.path.is_absolute() and artifact.path.parent == directory
        artifact = config.one_shot.observability.vm_resource_overhead
        if reference == "omitted":
            assert artifact is None
        elif reference in {"missing", "bad-sha"}:
            assert artifact is not None
            with pytest.raises(
                FileNotFoundError if reference == "missing" else ValueError
            ):
                artifact.read_verified()
        else:
            assert artifact is not None
            assert artifact.read_verified() == content


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
    scenario = load_scenario(preset)
    calls = []
    monkeypatch.setattr(product, "require_supported_runtimes", lambda *args: None)
    monkeypatch.setattr(product, "require_recipe_environment", lambda *args: None)
    monkeypatch.setattr(
        product, "_build_run_workflow", lambda **kwargs: SimpleNamespace(keep=False)
    )
    monkeypatch.setattr(product, "scenario_observers", lambda *args: ())

    def fail_load(*args, **kwargs):
        raise RuntimeError("load aborted")

    def report(path, *, workflow_completed):
        assert workflow_completed is False
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


@pytest.mark.parametrize("explicit_defaults", [False, True])
def test_public_one_shot_builder_selects_recipe_images(
    tmp_path, monkeypatch, explicit_defaults
):
    """Public one shot builder selects recipe images."""
    import nanolab.plans.one_shot_common as common
    from nanolab.config import EnvironmentConfig, ScenarioConfig

    preset = (
        Path(__file__).parents[2]
        / "src/nanolab/assets/presets/scenarios"
        / "one-shot-calibration.yaml"
    )
    data = yaml.safe_load(preset.read_text())
    if explicit_defaults:
        data.update(controlPlaneImage=None, functionImages={})
    config = ScenarioConfig.model_validate(data)
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
    )
    (tmp_path / "functions/rust/one-shot-workload").mkdir(parents=True)
    requests = []
    add = common.add_platform
    recipe_run = common.recipe_run_resource
    recipes = []

    def capture_recipe(**kwargs):
        recipes.append(Path(kwargs["recipe"]).name)
        return recipe_run(**kwargs)

    def capture(workflow, request, **kwargs):
        requests.append(request)
        return add(workflow, request, **kwargs)

    monkeypatch.setattr(
        "nanolab.plans.dispatch.default_tool_paths",
        lambda: SimpleNamespace(nanofaas_root=tmp_path),
    )
    monkeypatch.setattr(common, "add_platform", capture)
    monkeypatch.setattr(common, "recipe_run_resource", capture_recipe)
    compiled = build_scenario_workflow(
        config, environment, run_dir=tmp_path / "run"
    ).compile()
    assert compiled.tasks and requests
    assert recipes == ["one-shot-jvm.yaml"]
    for request in requests:
        assert request.recipe is not None
        assert request.recipe.functions == {
            "one-shot-workload": ("one-shot-workload", "rust")
        }
        assert all(
            function.image == "resolved-from-recipe" for function in request.functions
        )


def test_generic_comparison_keeps_supported_image_override():
    """Generic comparison keeps supported image override."""
    from nanolab.config import ScenarioConfig
    from nanolab.plans.runtime_comparison import _variant_image

    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "container",
            "functions": ["word-stats-java"],
            "controlPlaneImage": "registry/control-plane:chosen",
            "functionImages": {"word-stats-java": "registry/function:chosen"},
        }
    )
    assert _variant_image(config) == "registry/control-plane:chosen"
    assert config.function_images == {"word-stats-java": "registry/function:chosen"}
