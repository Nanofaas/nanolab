import json
import math
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from sonata_engine import TaskInputs

from nanolab.one_shot.supervision import CampaignSupervision
from nanolab.tasks.resource_tasks import acquire_now, release_now
from nanolab.workbench import components, loadgen, playbook
from nanolab.workbench.components import Deployed
from nanolab.workbench.platform import OPERATOR

INPUTS = TaskInputs._for_resource_values({}, ())


def _docker(monkeypatch, states: list, calls: list):
    """Answer `docker inspect` with each state in turn; record every argv."""

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        if argv[1] == "inspect":
            assert kwargs["timeout"] <= 2
            state = states.pop(0)
            if isinstance(state, BaseException):
                raise state
            return SimpleNamespace(stdout=state + "\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(loadgen.subprocess, "run", run)


def _run(states: list, monkeypatch, calls=None) -> loadgen.LoadgenRun:
    _docker(monkeypatch, states, [] if calls is None else calls)
    return loadgen.LoadgenRun(
        Deployed("operator", "loadgen-x", "loadgen", None), grace=5
    )


def test_loadgen_poll_tracks_container(monkeypatch):
    calls: list = []
    run = _run(["running 0", "exited 3"], monkeypatch, calls)
    assert run.poll() is None
    assert run.returncode is None
    run.checked = 0  # past the poll interval
    assert run.poll() == 3
    assert run.returncode == 3
    assert run.poll() == 3  # an exit is final: no more inspects
    assert (
        calls
        == [
            (
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}} {{.State.ExitCode}}",
                "wb-loadgen-x",
            )
        ]
        * 2
    )


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired("docker", 2),
        subprocess.CalledProcessError(1, "docker", stderr="No such object"),
        OSError("docker missing"),
    ],
)
def test_a_failed_inspect_is_unknown_not_an_exit(monkeypatch, failure):
    run = _run([failure, "exited 0"], monkeypatch)
    assert run.poll() is None  # never raises under the supervision lock
    assert run.error and run.returncode is None
    run.checked = 0
    assert run.poll() == 0
    assert run.error is None


def test_stop_caches_the_final_exit_code(monkeypatch):
    calls: list = []
    run = _run(["running 0", "exited 143"], monkeypatch, calls)
    run.stop()
    assert run.returncode == 143
    assert run.poll() == 143
    assert [c[1] for c in calls] == ["inspect", "stop", "inspect"]


def test_check_process_on_loadgen(monkeypatch):
    run = _run(["exited 0"], monkeypatch)
    with pytest.raises(RuntimeError, match="generator stopped unexpectedly: exit 0"):
        CampaignSupervision.check_process(run, expected_end=time.monotonic() + 100)
    # After the frozen load ended, a clean exit is accepted.
    CampaignSupervision.check_process(run, expected_end=time.monotonic() - 1)


def _deploys(monkeypatch, run_dir: Path, seen: list) -> None:
    def bootstrap(_provider, operations, *, role):
        action = operations[0].operation_id.removeprefix("workbench.")
        saved = json.loads(
            (run_dir / "workbench" / "vars" / f"{role}-{action}.json").read_text()
        )
        seen.append((action, saved["instances"][0]))

    monkeypatch.setattr(playbook, "run_bootstrap_operations", bootstrap)
    monkeypatch.setattr(components, "ensure_installed", lambda: Path("/coll"))


def test_generator_env_unchanged(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seen: list = []
    _deploys(monkeypatch, run_dir, seen)
    docker: list = []
    _docker(monkeypatch, ["running 0", "exited 143"], docker)
    resource = loadgen.loadgen_resource(OPERATOR, run_dir=run_dir, vus=4, duration=10.2)
    assert resource.title == "Own bounded one-shot k6 generator"
    first = acquire_now(resource, INPUTS)
    ((action, instance),) = seen
    assert action == "deploy"
    assert instance["component"] == "loadgen"
    assert instance["name"] == first.deployed.instance
    assert instance["config"] == {
        "script": "one-shot.js",
        "output_dir": str(run_dir.resolve()),
        "network": "host",
        "user": f"{os.getuid()}:{os.getgid()}",
        "env": {
            "ONE_SHOT_SCHEDULE": "/out/schedule.json",
            "ONE_SHOT_VUS": "4",
            "ONE_SHOT_DURATION": f"{math.ceil(10.2 + 60)}s",
        },
    }
    # The release stops k6 with process_exit_seconds, then undeploys it.
    release_now(resource, INPUTS, first)
    assert ("docker", "stop", "-t", "5", f"wb-{first.deployed.instance}") in docker
    assert seen[-1][0] == "undeploy"
    assert seen[-1][1]["name"] == first.deployed.instance
    # A loadgen runs once: every acquisition is a new instance.
    second = acquire_now(resource, INPUTS)
    assert second.deployed.instance != first.deployed.instance
    # A later process releases from the journalled value alone.
    assert resource.revive is not None
    revived = resource.revive(json.loads(json.dumps(asdict(second))))
    assert revived.deployed == second.deployed


def test_the_loadgen_runs_on_the_nanolab_host(tmp_path):
    node = playbook.WorkbenchHost("edge-1", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nanolab host"):
        loadgen.loadgen_resource(node, run_dir=tmp_path, vus=1, duration=1)


def test_an_unknown_loadgen_blocks_the_archive(tmp_path, monkeypatch):
    run = _run([OSError("docker missing")], monkeypatch)
    supervision = CampaignSupervision(tmp_path)
    supervision.writers.append(run)
    with pytest.raises(RuntimeError, match="still running or its state unknown"):
        supervision._require_writers_stopped()


def test_an_unreadable_exit_code_is_unknown_not_an_error(monkeypatch):
    run = _run(["exited ?"], monkeypatch)
    assert run.poll() is None  # never raises under the supervision lock
    assert run.error and run.returncode is None
