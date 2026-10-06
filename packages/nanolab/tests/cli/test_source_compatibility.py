"""Unsupported selections must fail before acquiring any external environment."""

from typer.testing import CliRunner

from nanolab.app.main import app
from nanolab.cli import product
from nanolab.workspace.paths import ToolPaths


def test_unsupported_runtime_fails_before_provisioning(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "functions/rust/echo").mkdir(parents=True)
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text("workflow: validate\nbackend: k8s\nfunctions: [echo-rust]\n")
    environment = tmp_path / "environment.yaml"
    environment.write_text(
        "provider: external\nroles:\n  stack:\n    host: vm.example\n"
    )
    monkeypatch.setattr(
        product, "default_tool_paths", lambda: ToolPaths.from_roots(source, tmp_path)
    )
    calls = []

    def provision(*args, **kwargs):
        calls.append("provision")
        raise AssertionError("source compatibility must precede provisioning")

    monkeypatch.setattr(product, "provision_environment", provision)
    result = CliRunner().invoke(
        app,
        [
            "run",
            str(scenario),
            "--environment",
            str(environment),
            "--run-dir",
            str(tmp_path / "run"),
        ],
    )
    assert result.exit_code != 0
    assert "Unsupported function runtime: rust" in result.output
    assert calls == []
