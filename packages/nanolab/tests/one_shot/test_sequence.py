"""The launcher owns sequence order, not another copy of the experiment engine."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def sequence_module():
    path = Path(__file__).parents[4] / "scripts/one-shot-sequence.py"
    spec = importlib.util.spec_from_file_location("one_shot_sequence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (None, ["step-oracle", "continuous-oracle", "continuous-ari"]),
        ("continuous-oracle", ["step-oracle", "continuous-oracle"]),
    ],
)
def test_runs_are_sequential_and_failure_stops_the_next_run(
    tmp_path, failure, expected
):
    module = sequence_module()
    jobs = []
    for name in ("step-oracle", "continuous-oracle", "continuous-ari"):
        freeze = tmp_path / (name + "-freeze")
        freeze.mkdir()
        script = """import json,sys
from pathlib import Path
name = Path(__file__).parent.name.removesuffix('-freeze')
with (Path(__file__).parents[1] / 'started.jsonl').open('a') as stream:
    stream.write(json.dumps(name) + '\\n')
sys.exit(9 if name == FAILURE else 0)
""".replace("FAILURE", repr(failure))
        (freeze / "source-used.py").write_text(script)
        jobs.append((name, freeze))
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            module.run_freezes(jobs, tmp_path, sys.executable)
    else:
        module.run_freezes(jobs, tmp_path, sys.executable)
    assert [
        json.loads(line)
        for line in (tmp_path / "started.jsonl").read_text().splitlines()
    ] == expected
    status = json.loads((tmp_path / "sequence-status.json").read_text())
    assert status["state"] == ("failed" if failure else "complete")


def sequence_inputs(tmp_path, *, replayed, parent):
    """Write a minimal input set; only the shapes `inputs` reads are real."""
    physical = tmp_path / "samples/physical-window/physical.jsonl"
    physical.parent.mkdir(parents=True)
    physical.write_text("{}\n")
    module = sequence_module()
    observations = tmp_path / "observations.json"
    observations.write_text(
        json.dumps({"sourcePhysicalSha256": module.digest(physical)})
    )
    tuning = tmp_path / "tuning"
    tuning.mkdir()
    minute = {"period_seconds": 60, "windows": [{}] * 54}
    for name in ("trace.json", "selected.json", "input-context.json"):
        (tuning / name).write_text(json.dumps(minute))
    if replayed:
        (tuning / "arrival-profile.json").write_text("{}")
    (tmp_path / "parent").mkdir()
    (tmp_path / "runtime").mkdir()
    config = tmp_path / "inputs.json"
    config.write_text(
        json.dumps(
            {
                "runtimeSource": "runtime",
                "stepTuning": "tuning",
                "continuousTuning": "tuning",
                "continuousParentFreeze": "parent" if parent else None,
                "observations": "observations.json",
                "physicalSamples": "samples/physical-window/physical.jsonl",
            }
        )
    )
    return module, config


def test_a_load_tuned_in_minutes_needs_no_parent_freeze(tmp_path):
    module, config = sequence_inputs(tmp_path, replayed=False, parent=False)
    result = module.inputs(config)
    assert result["continuousParentFreeze"] is None
    assert result["continuousTuning"] == tmp_path / "tuning"


@pytest.mark.parametrize(("replayed", "parent"), [(True, False), (False, True)])
def test_replay_and_parent_freeze_go_together(tmp_path, replayed, parent):
    module, config = sequence_inputs(tmp_path, replayed=replayed, parent=parent)
    with pytest.raises(ValueError, match="parent freeze"):
        module.inputs(config)
