"""The loadgen mounts the run directory the plan named, wherever it runs from."""

import json
from pathlib import Path

from sonata_engine import TaskInputs
from sonata_tasks.ansible import AnsiblePlaybookTask

from nanolab.tasks.resource_tasks import acquire_now
from nanolab.workbench import components, loadgen
from nanolab.workbench.platform import OPERATOR
from tests.workbench.playbook_fakes import host_of


def test_generator_captures_invocation_directory(tmp_path, monkeypatch):
    run_dir = tmp_path / "run data"
    run_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    resource = loadgen.loadgen_resource(
        OPERATOR, run_dir=Path("run data"), vus=1, duration=1
    )
    monkeypatch.chdir(tmp_path.parent)
    configs = []

    def deploy(task, inputs):
        role = host_of(task)
        saved = run_dir / "workbench" / "vars" / f"{role}-deploy.json"
        configs.append(json.loads(saved.read_text())["instances"][0]["config"])

    monkeypatch.setattr(AnsiblePlaybookTask, "run", deploy)
    monkeypatch.setattr(components, "ensure_installed", lambda: Path("/coll"))
    acquire_now(resource, TaskInputs._for_resource_values({}, ()))
    (config,) = configs
    # k6 reads the schedule inside the container, where the run dir is /out.
    assert config["output_dir"] == str(run_dir.resolve())
    assert config["env"]["ONE_SHOT_SCHEDULE"] == "/out/schedule.json"
