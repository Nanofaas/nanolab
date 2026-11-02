import json
from contextlib import nullcontext

import pytest
from typer.testing import CliRunner

from nanolab.app.main import app
from nanolab.comparison.module_cost import prepare_campaign
from tests.cli.test_command_surface import _sonata_workflow


def inputs(tmp_path):
    campaign = prepare_campaign(tmp_path, tmp_path / "key.pub", 1)
    block = campaign / json.loads((campaign / "manifest.json").read_text())["blocks"][0]
    return [
        "run",
        str(block / "scenario.yaml"),
        "--environment",
        str(block / "environment.yaml"),
        "--run-dir",
        str(block / "results"),
    ]


@pytest.mark.parametrize("flag", ["--keep", "--resume", "--only=1"])
def test_module_cost_rejects_partial_or_retained_runs_before_provisioning(
    tmp_path, flag, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not provision")

    monkeypatch.setattr(
        "nanolab.application.provisioning.provision_environment", forbidden
    )
    result = CliRunner().invoke(app, [*inputs(tmp_path), flag])
    assert result.exit_code != 0
    assert "complete uninterrupted" in result.output


@pytest.mark.nanofaas
def test_module_cost_cli_resolves_remote_urls_and_owns_tunnel(tmp_path, monkeypatch):
    seen = []
    workflow = _sonata_workflow()
    monkeypatch.setattr(
        "nanolab.application.provisioning.provision_environment",
        lambda *a, **kw: nullcontext(),
    )
    monkeypatch.setattr(
        "nanolab.cli.product.resolve_loadtest_urls",
        lambda *a, **kw: ("http://stack:30080", "http://stack:30090"),
    )

    def tunnel(_env, url, **kwargs):
        seen.append(url)
        return nullcontext("http://127.0.0.1:54321")

    monkeypatch.setattr("nanolab.cli.product.prometheus_over_ssh", tunnel)

    def build(*args, **kwargs):
        seen.append((kwargs["control_plane_url"], kwargs["prometheus_url"]))
        return workflow

    monkeypatch.setattr("nanolab.cli.product.build_scenario_workflow", build)
    monkeypatch.setattr(workflow, "run", lambda **kwargs: None)
    result = CliRunner().invoke(app, inputs(tmp_path))
    assert result.exit_code == 0, (result.output, result.exception)
    assert seen == [
        "http://stack:30090",
        ("http://stack:30080", "http://127.0.0.1:54321"),
    ]


def test_owned_module_cost_rejects_foreign_endpoint_override(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not provision")

    monkeypatch.setattr(
        "nanolab.application.provisioning.provision_environment", forbidden
    )
    result = CliRunner().invoke(
        app, [*inputs(tmp_path), "--control-plane-url=http://other:8080"]
    )
    assert result.exit_code != 0
    assert "owns its endpoints" in result.output
