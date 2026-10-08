import pytest
from typer.testing import CliRunner

import nanolab.cli.product as product
from nanolab.app.main import app


@pytest.mark.parametrize(
    ("command", "options"),
    [
        ("run", ["--keep"]),
        ("run", ["--resume"]),
        ("run", ["--teardown"]),
        ("run", ["--only", "x"]),
        ("plan", ["--from", "x"]),
        ("plan", ["--until", "x"]),
        ("run", ["--control-plane-url", "http://localhost:1"]),
        ("plan", ["--prometheus-url", "http://localhost:2"]),
    ],
)
def test_forbidden_options_rejected_before_acquisition(
    tmp_path, monkeypatch, command, options
):
    scenario = tmp_path / "contract.yaml"
    scenario.write_text(
        "workflow: contract\nbackend: container\n"
        "functions: [word-stats]\ncontract: {}\n"
    )

    def unexpected(*args, **kwargs):
        pytest.fail("forbidden options reached platform acquisition")

    monkeypatch.setattr(product, "_provisioning_context", unexpected)
    monkeypatch.setattr(product, "_workflow", unexpected)
    monkeypatch.setattr(product, "_run_teardown", unexpected)
    result = CliRunner().invoke(app, [command, str(scenario), *options])
    assert result.exit_code == 2, result.output
    assert "contract" in result.output.lower()


@pytest.mark.parametrize(
    "name", ["artifact-contract-container", "artifact-contract-smoke-container"]
)
def test_installed_contract_preset_resolution(name):
    from nanolab.cli.catalogue import _scenario
    from nanolab.workspace.paths import discover_tool_root

    config = _scenario(discover_tool_root() / "scenarios" / f"{name}.yaml")
    assert config.workflow == "contract"
    assert config.contract is not None
    assert config.backend == "container"


@pytest.mark.parametrize("failure", [False, True])
def test_contract_skips_control_plane_provisioning_and_finalizes_after_release(
    tmp_path, monkeypatch, failure
):
    from sonata_engine import Resource, Task, TaskInputs, TaskOutcome, Workflow

    from nanolab.workspace.paths import ToolPaths, discover_tool_root

    run_dir = tmp_path / "run"
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        "workflow: contract\nbackend: container\n"
        "functions: [word-stats]\ncontract: {}\n"
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "build.gradle").write_text("version = '1.0.0'\n")
    monkeypatch.setattr(
        product,
        "default_tool_paths",
        lambda: ToolPaths.from_roots(
            source, discover_tool_root(), workspace_root=tmp_path
        ),
    )
    events = []

    def release(inputs, value):
        events.append("released")
        if failure:
            raise RuntimeError("release failed")

    resource = Resource(
        title="owned test resource", acquire=lambda inputs: None, release=release
    )

    class Invoke(Task[None]):
        title = "Invoke packaged contracts"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            events.append("cases")
            return TaskOutcome()

    workflow = Workflow(workflow_id="contract-test")
    workflow.add(Invoke(), requires=(resource,))

    def build(*args, **kwargs):
        assert kwargs["scenario_path"] == scenario
        return workflow

    def unexpected(*args, **kwargs):
        pytest.fail("contract reached control-plane provisioning")

    def finalize(root, *, executor):
        # This test covers product/engine lifecycle; finalizer integrity has real
        # filesystem/Docker-boundary coverage in tests/plans/test_contract.py.
        assert events == ["cases", "released"]
        events.append("qualified")
        root.mkdir(parents=True, exist_ok=True)
        marker = root / "qualification.json"
        marker.write_text("{}")
        return marker

    import nanolab.plans.contract as contract

    monkeypatch.setattr(product, "_workflow", build)
    monkeypatch.setattr(product, "_provisioning_context", unexpected)
    monkeypatch.setattr(contract, "finalize_contract_run", finalize)
    result = CliRunner().invoke(app, ["run", str(scenario), "--run-dir", str(run_dir)])
    assert result.exit_code == (1 if failure else 0), result.output
    assert (run_dir / "qualification.json").exists() is not failure
    assert events == (
        ["cases", "released"] if failure else ["cases", "released", "qualified"]
    )


def test_remote_provider_rejected_before_acquisition(tmp_path, monkeypatch):
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        "workflow: contract\nbackend: container\n"
        "functions: [word-stats]\ncontract: {}\n"
    )
    environment = tmp_path / "environment.yaml"
    environment.write_text(
        "provider: external\nroles:\n  stack: {host: example.invalid}\n"
    )

    def unexpected(*args, **kwargs):
        pytest.fail("remote provider reached platform")

    monkeypatch.setattr(product, "_workflow", unexpected)
    result = CliRunner().invoke(
        app, ["plan", str(scenario), "--environment", str(environment)]
    )
    assert result.exit_code == 2, result.output
    assert "local" in result.output
