"""Exercise the wheel from an operator workspace outside its source checkout."""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def test_installed_wheel_catalogue_presets_and_writable_workspace(
    tmp_path: Path,
) -> None:
    package = Path(__file__).resolve().parents[1]
    build_source = tmp_path / "build-source"
    build_source.mkdir()
    for name in ("pyproject.toml", "LICENSE"):
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
    with zipfile.ZipFile(next((tmp_path / "dist").glob("*.whl"))) as wheel:
        wheel.extractall(installed)
    operator = tmp_path / "operator"
    operator.mkdir()
    source = Path(os.environ["NANOFAAS_ROOT"])
    script = """
from pathlib import Path
from typer.testing import CliRunner
from nanolab.app.main import app
from nanolab.cli.product import _scenario, _environment
from nanolab.tui.app import NanofaasTUI
from nanolab.workspace.paths import default_tool_paths
import nanolab
assert Path(nanolab.__file__).is_relative_to(Path("../installed").resolve())
for probe in ("retry_hint_probe.py", "retry_backoff_burst.py"):
    assert (Path(nanolab.__file__).parent / "assets/validation" / probe).is_file()
listing = CliRunner().invoke(app, ["list"])
assert listing.exit_code == 0, listing.output
assert "deployment-lifecycle-container.yaml" in listing.output, listing.output
scenario = next(
    Path(line) for line in listing.output.splitlines()
    if line.endswith("/deployment-lifecycle-container.yaml")
)
config = _scenario(scenario)
assert config.recipe_profile.is_file()
planned = CliRunner().invoke(app, [
    "plan", str(scenario.parent / "cli-contract-container.yaml"),
    "--only", "list-functions",
])
assert planned.exit_code == 0, (planned.output, planned.exception)
workflows = CliRunner().invoke(app, ["workflow"])
assert workflows.exit_code == 0 and "validate" in workflows.output, workflows.output
selected = []
def choose(*args, **kwargs):
    choices = kwargs["choices"]
    selected.extend(choices)
    return next(
        choice.value for choice in choices
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
custom.write_text("provider: local\\n")
assert NanofaasTUI(choose=choose)._select_environment() == custom
import os
os.environ["NANOLAB_WORKSPACE"] = str(Path.cwd() / "other-workspace")
assert default_tool_paths().runs_dir == Path.cwd() / "other-workspace/runs"
"""
    env = {**os.environ, "PYTHONPATH": str(installed), "NANOFAAS_ROOT": str(source)}
    env.pop("NANOLAB_WORKSPACE", None)
    checked = subprocess.run(
        [sys.executable, "-c", script],
        cwd=operator,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_bundled_presets_match_public_source_resources() -> None:
    package = Path(__file__).resolve().parents[1]
    bundled = package / "src/nanolab/assets/presets"
    for directory in ("scenarios-v2", "recipes", "environments", "scenarios/payloads"):
        tracked = subprocess.run(
            ["git", "ls-files", directory],
            cwd=package,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert tracked
        assert {path.name for path in (bundled / directory).iterdir()} == {
            Path(name).name for name in tracked
        }
        for name in tracked:
            assert (bundled / name).read_bytes() == (package / name).read_bytes(), name
