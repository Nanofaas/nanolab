"""Exercise k6 module initialization through the actual Sonata resource."""

import json
import shutil
from pathlib import Path

import pytest
from sonata_engine import Task, TaskOutcome, Workflow

from nanolab.tasks.one_shot import experiment


@pytest.mark.skipif(
    shutil.which("k6") is None, reason="Requires the real k6 executable"
)
@pytest.mark.parametrize("relative", [True, False])
@pytest.mark.parametrize("change_directory", [True, False])
def test_k6_reads_schedule_with_relative_absolute_and_spaced_paths(
    tmp_path, monkeypatch, relative, change_directory
):
    operator = tmp_path / "operator directory"
    run_dir = operator / "run data"
    run_dir.mkdir(parents=True)
    (run_dir / "schedule.json").write_text(json.dumps([{"originalId": "probe"}]))
    monkeypatch.chdir(operator)
    resource = experiment.generator_resource(
        run_dir=Path("run data") if relative else run_dir, vus=1, duration=1
    )
    original_popen = experiment.subprocess.Popen

    def inspect_module(argv, **kwargs):
        # Execute the same module and environment, without sending any requests.
        assert argv[:2] == ("k6", "run")
        return original_popen(
            ("k6", "inspect", "--include-system-env-vars", argv[-1]), **kwargs
        )

    monkeypatch.setattr(experiment.subprocess, "Popen", inspect_module)
    if change_directory:
        other = tmp_path / "other directory"
        other.mkdir()
        monkeypatch.chdir(other)

    class Inspect(Task):
        title = "Inspect actual k6 module"

        def run(self, inputs):
            child = inputs.resource(resource)
            assert child.wait(timeout=15) == 0, (
                run_dir / "generator.stderr"
            ).read_text()
            return TaskOutcome()

    workflow = Workflow("generator-path-regression")
    workflow.add(Inspect(), requires=(resource,))
    workflow.run()
    options = json.loads((run_dir / "generator.jsonl").read_text())
    assert options["scenarios"]["originals"]["iterations"] == 1


def test_generator_captures_invocation_directory(tmp_path, monkeypatch):
    """Keep path coverage in environments where k6 is unavailable."""
    from sonata_engine import TaskInputs

    run_dir = tmp_path / "run data"
    run_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    resource = experiment.generator_resource(
        run_dir=Path("run data"), vus=1, duration=1
    )
    monkeypatch.chdir(tmp_path.parent)

    def launch(_argv, **kwargs):
        assert Path(kwargs["env"]["ONE_SHOT_SCHEDULE"]) == run_dir / "schedule.json"
        raise OSError("intentional launch stop")

    monkeypatch.setattr(experiment.subprocess, "Popen", launch)
    with pytest.raises(OSError, match="intentional launch stop"):
        resource.acquire(TaskInputs._for_resources({}, set()))
