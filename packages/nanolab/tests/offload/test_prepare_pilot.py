"""The B4 preparation writes runnable inputs without owning infrastructure."""

import json
import os
import runpy
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from nanolab.application.inputs import load_environment, load_scenario
from nanolab.config.environment import require_experiment_environment


def prepare(output_root: Path, ssh_key: Path) -> Path:
    script = Path(__file__).parents[4] / "scripts/b4-prepare-pilot.py"
    return runpy.run_path(str(script))["prepare"](output_root, ssh_key)


def test_pilot_configs_use_distinct_hybrid_roles_and_matched_work(tmp_path):
    root = prepare(tmp_path, Path("/home/operator/.ssh/azure.pub"))
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["ratesProvisional"] is True
    assert [(arm["rate"], arm["mode"]) for arm in manifest["arms"]] == [
        (20, "disabled"),
        (20, "pressure"),
        (20, "cloud"),
        (100, "pressure"),
        (100, "disabled"),
        (100, "cloud"),
        (300, "disabled"),
        (300, "pressure"),
        (300, "cloud"),
    ]
    names = []
    for arm in manifest["arms"]:
        scenario = load_scenario(Path(arm["scenario"]))
        environment = load_environment(Path(arm["environment"]))
        require_experiment_environment(environment)
        assert scenario.offload_experiment is not None
        assert environment.azure is not None
        function_limits = scenario.resources["word-stats-java"].limits
        control_limits = scenario.resources["control-plane"].limits
        assert function_limits is not None and control_limits is not None
        assert scenario.offload_experiment.mode == arm["mode"]
        assert scenario.offload_experiment.rate == arm["rate"]
        assert scenario.offload_experiment.duration_seconds == 180
        assert scenario.offload_experiment.warmup_seconds == 60
        assert function_limits.cpu == 2
        assert function_limits.memory_mib == 512
        assert control_limits.cpu == 2
        assert control_limits.memory_mib == 2048
        assert environment.provider == "multipass"
        assert environment.role_provider("cloud") == "azure"
        assert environment.roles["stack"].cpus == 4
        assert environment.roles["loadgen"].cpus == 2
        assert environment.roles["loadgen"].memory == "4G"
        assert environment.azure.vm_size == "Standard_D8s_v5"
        assert environment.azure.priority == "Spot"
        assert environment.azure.resource_group == "maurinoRicerca-rg"
        assert environment.azure.location == "westeurope"
        assert environment.azure.operator_source_cidr == "auto"
        assert environment.azure.ssh_key_path == "/home/operator/.ssh/azure.pub"
        assert all(target.host is None for target in environment.roles.values())
        names.extend(target.name for target in environment.roles.values())
        assert not Path(arm["runDir"]).exists()
        assert shlex.split(arm["command"]) == [
            "nanolab",
            "run",
            arm["scenario"],
            "--environment",
            arm["environment"],
            "--run-dir",
            arm["runDir"],
        ]
    assert len(names) == len(set(names)) == 27
    second = prepare(tmp_path, Path("/home/operator/.ssh/azure.pub"))
    assert second != root
    assert json.loads((root / "manifest.json").read_text()) == manifest


@pytest.mark.parametrize("fail", [False, True])
def test_generated_shell_preserves_argument_paths_and_stops_on_failure(tmp_path, fail):
    root = prepare(tmp_path / "inputs $(touch INJECTED)", Path("/keys/azure.pub"))
    binary = tmp_path / "bin"
    binary.mkdir()
    launcher = binary / "nanolab"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['CALLS'], 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(int(os.environ['FAIL']))\n"
    )
    launcher.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    result = subprocess.run(
        ["/bin/sh", str(root / "commands.sh")],
        cwd=tmp_path,
        check=False,
        env={
            **os.environ,
            "PATH": str(binary),
            "CALLS": str(calls),
            "FAIL": "7" if fail else "0",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == (7 if fail else 0), result.stderr
    actual = [json.loads(line) for line in calls.read_text().splitlines()]
    manifest = json.loads((root / "manifest.json").read_text())
    expected = [shlex.split(arm["command"])[1:] for arm in manifest["arms"]]
    assert actual == (expected[:1] if fail else expected)
    assert not (tmp_path / "INJECTED").exists()


def test_relative_key_is_rejected_before_writing_preparation(tmp_path):
    output = tmp_path / "pilot"
    with pytest.raises(ValueError, match="absolute"):
        prepare(output, Path("azure.pub"))
    assert not output.exists()
