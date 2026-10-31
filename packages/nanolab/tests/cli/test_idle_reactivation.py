import pytest
import yaml
from typer.testing import CliRunner

from nanolab.app.main import app
from tests.config.test_idle_reactivation import idle_scenario


@pytest.mark.parametrize("option", ["--resume", "--keep", "--only=1"])
def test_b6_rejects_partial_or_kept_runs_before_acquiring_machines(tmp_path, option):
    scenario, environment = tmp_path / "b6.yaml", tmp_path / "env.yaml"
    scenario.write_text(yaml.safe_dump(idle_scenario()))
    environment.write_text(
        yaml.safe_dump(
            {
                "provider": "external",
                "roles": {"stack": {"host": "stack"}, "loadgen": {"host": "loadgen"}},
            }
        )
    )
    result = CliRunner().invoke(
        app, ["run", str(scenario), "--environment", str(environment), option]
    )
    assert result.exit_code != 0
    assert "complete uninterrupted" in result.output
