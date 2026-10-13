"""The saved memory configurator preserves CPU, workload inputs and source data."""

import hashlib
import json
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import yaml

from nanolab.config.scenario import ScenarioConfig


def configure(tmp_path, *, memory_mib=None, existing_output=False):
    document = yaml.safe_load(
        files("nanolab")
        .joinpath("assets/presets/scenarios/one-shot-multifunction-calibration.yaml")
        .read_text()
    )
    # Original campaign: five 64-MiB functions and balanced 1/1/2/2/3/7 vectors.
    for fn in document["oneShot"]["functions"].values():
        fn.update(memoryMiB=64, maxReplicas=5)
    for node, pool in zip(
        document["oneShot"]["nodes"], [320, 320, 640, 640, 960, 2240], strict=True
    ):
        node["memoryCapacityMiB"] = pool
        for limit in node["functionLimits"].values():
            limit.update(memoryMiB=64, maxReplicas=pool // 64)
    if memory_mib is not None:
        document["oneShot"]["nodes"][0]["memoryMiB"] = memory_mib
    source = tmp_path / "original.yaml"
    source.write_text(yaml.safe_dump(document))
    output = tmp_path / "new.yaml"
    if existing_output:
        output.write_text("retained artifact\n")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[4] / "scripts/one-shot-function-memory.py"),
            "--input",
            str(source),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert yaml.safe_load(source.read_text()) == document
    return document, output, result


def test_script_sets_memory_and_pools_without_changing_cpu_or_other_inputs(tmp_path):
    original, output, result = configure(tmp_path)
    assert result.returncode == 0, result.stderr
    generated = yaml.safe_load(output.read_text())
    model = ScenarioConfig.model_validate(generated)
    assert model.one_shot is not None
    assert [node.memory_capacity_mib for node in model.one_shot.nodes] == [
        832,
        832,
        1664,
        1664,
        2496,
        5824,
    ]
    expected = {
        "one-shot-arithmetic-rust": 64,
        "one-shot-sort-rust": 128,
        "one-shot-memory-rust": 256,
        "one-shot-matrix-rust": 128,
        "one-shot-mixed-rust": 256,
    }
    assert {
        name: fn.memory_mib for name, fn in model.one_shot.functions.items()
    } == expected
    for node, before in zip(
        generated["oneShot"]["nodes"], original["oneShot"]["nodes"], strict=True
    ):
        for key in before.keys() - {"memoryCapacityMiB", "functionLimits"}:
            assert node[key] == before[key]
        for name, limit in node["functionLimits"].items():
            assert limit["cpu"] == before["functionLimits"][name]["cpu"]
            assert limit["memoryMiB"] == expected[name]
    for name, fn in generated["oneShot"]["functions"].items():
        for key in fn.keys() - {"memoryMiB", "maxReplicas"}:
            assert fn[key] == original["oneShot"]["functions"][name][key]
    for key in original["oneShot"].keys() - {"nodes", "functions"}:
        assert generated["oneShot"][key] == original["oneShot"][key]
    receipt = json.loads(output.with_suffix(".memory.json").read_text())
    assert receipt["scenarioSha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert receipt["qualified"] is False
    assert receipt["loadStatus"] == "pending-python-tuning"


def test_script_preserves_existing_output_instead_of_overwriting_a_run(tmp_path):
    _original, output, result = configure(tmp_path, existing_output=True)
    assert result.returncode != 0
    assert "output scenario or memory receipt already exists" in result.stderr
    assert output.read_text() == "retained artifact\n"
    assert not output.with_suffix(".memory.json").exists()


def test_script_rejects_larger_pool_that_leaves_insufficient_vm_ram(tmp_path):
    _original, output, result = configure(tmp_path, memory_mib=1536)
    assert result.returncode != 0
    assert "collector memory reservation leaves no platform memory" in result.stderr
    assert not output.exists()
