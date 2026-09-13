"""Exercise shared cleanup failure gates and durable retries with real Sonata."""

import json
from functools import partial

import pytest
from sonata_engine import JournalConfig, Resource, Task, TaskOutcome, Workflow

from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.soak.owned_functions import (
    FunctionOwnership,
    delete_owned_function,
    journaled_function_resource,
)
from nanolab.tasks.soak.retention import CleanupState, journaled_compose_resource
from nanolab.tasks.soak.teardown import TeardownSoakTask, verify_function_owner

PROJECT = "soak-" + "a" * 32
IMAGE = "registry/cp@sha256:" + "b" * 64
START = "2026-09-13T10:00:00.000000000Z"


class Measurement(Task):
    title = "Measure and publish report"

    def __init__(self, events, fail):
        self.events, self.fail = events, fail

    def run(self, inputs):
        self.events.append("report")
        if self.fail:
            raise RuntimeError("measurement failed after report")
        return TaskOutcome(value=None)


def command(events):
    def execute(argv, cwd, env):
        if "inspect" in argv:
            return json.dumps(
                [
                    {
                        "Id": "c" * 64,
                        "Config": {
                            "Image": IMAGE,
                            "Labels": {
                                "com.docker.compose.project": PROJECT,
                                "com.docker.compose.service": "control-plane",
                            },
                        },
                        "State": {"Running": True, "StartedAt": START},
                        "NetworkSettings": {
                            "Ports": {
                                "8080/tcp": [
                                    {"HostIp": "127.0.0.1", "HostPort": "18080"},
                                ]
                            }
                        },
                    }
                ]
            ).encode()
        if "ps" in argv:
            return ("c" * 64).encode()
        assert "down" in argv
        assert PROJECT in argv
        assert "--remove-orphans" not in argv
        events.append("down")
        return b""

    return execute


def build(tmp_path, events, request, *, keep=False, fail=False, acquire_error=False):
    state = CleanupState(JournalConfig(path=tmp_path / "cleanup.jsonl"))
    path = tmp_path / "compose.yaml"
    path.write_text("services: {}\n")
    project = DockerComposeProject(PROJECT, path, "http://127.0.0.1:18081", build=False)
    compose = journaled_compose_resource(
        Resource(
            title="Acquire Compose",
            acquire=lambda inputs: project,
            release=lambda *args: events.append("down"),
        ),
        project=project,
        cwd=tmp_path,
        cleanup_state=state,
        command=command(events),
    )

    def acquire(inputs):
        if acquire_error:
            raise RuntimeError("HTTP 409 existing name")
        events.append("registered")

    function = journaled_function_resource(
        Resource(
            title="Acquire function",
            acquire=acquire,
            release=lambda *args: None,
            requires=(compose,),
        ),
        ownership=lambda: FunctionOwnership(
            "fn",
            "http://127.0.0.1:18080",
            IMAGE,
            PROJECT,
            str(tmp_path),
            "c" * 64,
            START,
        ),
        cleanup_state=state,
        verify_owner=partial(verify_function_owner, command=command(events)),
        request=request,
    )
    return Workflow(workflow_id="soak", keep=keep).add(
        Measurement(events, fail),
        requires=(function,),
    )


def teardown(tmp_path, events, request):
    commands = command(events)
    delete = partial(
        delete_owned_function,
        request=request,
        verify_owner=partial(verify_function_owner, command=commands),
    )
    return (
        Workflow(workflow_id="teardown")
        .add(
            TeardownSoakTask(tmp_path, command=commands, delete_function=delete),
        )
        .run()
    )


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_retained_resources_release_from_journal_after_report(tmp_path, keep, fail):
    events = []

    def request(method, *args):
        events.append(method)
        return 204 if method == "DELETE" else 404

    workflow = build(tmp_path, events, request, keep=keep, fail=fail)
    if fail:
        with pytest.raises(RuntimeError, match="measurement failed after report"):
            workflow.run()
    else:
        workflow.run()
    if keep:
        assert "down" not in events
        assert "DELETE" not in events
        del workflow
        teardown(tmp_path, events, request)
    assert events.count("down") == 1
    assert events.index("report") < events.index("DELETE") < events.index("down")
    previous = list(events)
    teardown(tmp_path, events, request)
    assert events == previous


def test_function_cleanup_failure_keeps_cp_and_fresh_process_can_retry(tmp_path):
    events = []
    workflow = build(tmp_path, events, lambda *args: 409)
    with pytest.raises(RuntimeError, match=r"cleanup|DELETE"):
        workflow.run()
    assert "down" not in events
    del workflow
    assert (tmp_path / "cleanup.jsonl").is_file()
    teardown(tmp_path, events, lambda *args: 404)
    assert events.count("down") == 1


def test_unknown_failed_acquisition_preserves_cp_and_blocks_standalone(tmp_path):
    events = []
    workflow = build(
        tmp_path,
        events,
        lambda *args: pytest.fail("unexpected HTTP"),
        acquire_error=True,
    )
    with pytest.raises(RuntimeError, match=r"409|cleanup"):
        workflow.run()
    assert "down" not in events
    del workflow
    with pytest.raises(ValueError, match="invalid owned function record fields"):
        teardown(tmp_path, events, lambda *args: pytest.fail("unexpected HTTP"))
    assert "down" not in events
