"""Smoke-test installed catalogue, presets and writable outputs without a checkout."""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> None:
    """Run entirely within a disposable operator workspace."""
    installed_root = (
        Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(sys.prefix).resolve()
    )
    with TemporaryDirectory(prefix="nanolab-wheel-smoke-") as temporary:
        workspace = Path(temporary)
        source = workspace / "nanofaas"
        source.mkdir()
        (source / "build.gradle").write_text("version = '0.18.0'\n")
        (source / "settings.gradle").write_text("rootProject.name = 'nanofaas'\n")
        for runtime, family, name in (
            ("go", "echo", "echo"),
            ("java", "word-stats", "word-stats-java"),
        ):
            function = source / "functions" / runtime / family
            function.mkdir(parents=True)
            (function / "function.yaml").write_text(f"name: {name}\n")
        os.environ["NANOFAAS_ROOT"] = str(source)
        os.environ.pop("NANOLAB_WORKSPACE", None)
        operator = workspace / "operator"
        operator.mkdir()
        os.chdir(operator)

        import nanolab
        from nanolab.app.main import app
        from nanolab.cli.product import _environment, _scenario
        from nanolab.functions.catalog import list_functions
        from nanolab.tui.app import NanofaasTUI
        from nanolab.workspace.paths import default_tool_paths
        from typer.testing import CliRunner

        assert {item.key for item in list_functions()} == {
            "echo-go",
            "word-stats-java",
            "tool-metrics-echo",
        }
        assert Path(nanolab.__file__).is_relative_to(installed_root)
        for probe in ("retry_hint_probe.py", "retry_backoff_burst.py"):
            assert (
                Path(nanolab.__file__).parent / "assets/validation" / probe
            ).is_file()
        listing = CliRunner().invoke(app, ["list"])
        assert listing.exit_code == 0, listing.output
        assert "deployment-lifecycle-container.yaml" in listing.output, listing.output
        scenario = next(
            Path(line)
            for line in listing.output.splitlines()
            if line.endswith("/deployment-lifecycle-container.yaml")
        )
        config = _scenario(scenario)
        assert config.recipe_profile.is_file()
        planned = CliRunner().invoke(
            app,
            [
                "plan",
                str(scenario.parent / "cli-contract-container.yaml"),
                "--only",
                "list-functions",
            ],
        )
        assert planned.exit_code == 0, (planned.output, planned.exception)
        workflows = CliRunner().invoke(app, ["workflow"])
        assert workflows.exit_code == 0 and "validate" in workflows.output, (
            workflows.output
        )
        selected = []

        def choose(*args, **kwargs):
            choices = kwargs["choices"]
            selected.extend(choices)
            return next(
                choice.value
                for choice in choices
                if Path(choice.value).name == "local.yaml"
            )

        environment = NanofaasTUI(choose=choose)._select_environment()
        assert _environment(environment).provider == "local"
        assert any(choice.value == "setup:azure" for choice in selected)
        for name in NanofaasTUI.SCENARIO_FILES.values():
            assert (scenario.parent / name).is_file(), name
        paths = default_tool_paths()
        assert paths.runs_dir == Path.cwd() / "runs", paths
        assert paths.scenario_payloads_dir.joinpath("echo-sample.json").is_file()
        paths.runs_dir.mkdir()
        (paths.runs_dir / "receipt.txt").write_text("operator output")
        custom = Path.cwd() / "environments/local.yaml"
        custom.parent.mkdir()
        custom.write_text("provider: local\n")
        assert NanofaasTUI(choose=choose)._select_environment() == custom

        os.environ["NANOLAB_WORKSPACE"] = str(Path.cwd() / "other-workspace")
        assert default_tool_paths().runs_dir == Path.cwd() / "other-workspace/runs"


if __name__ == "__main__":
    main()
