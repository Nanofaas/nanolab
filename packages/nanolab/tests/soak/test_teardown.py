"""Replay a real retained Sonata run without Docker or HTTP side effects."""

import json
from types import SimpleNamespace

import pytest
from sonata_engine import JournalConfig, Resource, Task, TaskOutcome, Workflow

from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.soak.owned_functions import (
    FunctionOwnership,
    journaled_function_resource,
)
from nanolab.tasks.soak.retention import CleanupState, journaled_compose_resource
from nanolab.tasks.soak.teardown import TeardownSoakTask, build_teardown_workflow

PROJECT = "soak-" + "a" * 32
IMAGE = "registry/cp@sha256:" + "b" * 64


class Consume(Task):
    title = "Complete measurements"

    def run(self, inputs):
        return TaskOutcome(value=None)


def retained(tmp_path):
    state = CleanupState(JournalConfig(path=tmp_path / "cleanup.jsonl"))
    path = tmp_path / "compose.yaml"
    path.write_text("services: {}\n")
    project = DockerComposeProject(
        name=PROJECT,
        file=path,
        ready_url="http://127.0.0.1:18081",
        build=False,
    )
    compose = journaled_compose_resource(
        Resource(
            title="Acquire owned Compose",
            acquire=lambda inputs: project,
            release=lambda *args: pytest.fail("unexpected early release"),
        ),
        project=project,
        cwd=tmp_path,
        cleanup_state=state,
        command=lambda *args: pytest.fail("unexpected early cleanup"),
    )
    owner = FunctionOwnership(
        name="fn",
        api_endpoint="http://127.0.0.1:18080",
        control_plane_image=IMAGE,
        project_name=PROJECT,
        cwd=str(tmp_path),
        control_plane_container_id="c" * 64,
        control_plane_started_at="2026-09-13T10:00:00.000000000Z",
    )
    function = journaled_function_resource(
        Resource(
            title="Acquire owned function",
            acquire=lambda inputs: None,
            release=lambda *args: None,
            requires=(compose,),
        ),
        ownership=owner,
        cleanup_state=state,
        verify_owner=lambda owner: None,
        request=lambda *args: 404,
    )
    Workflow(workflow_id="soak", keep=True).add(
        Consume(),
        requires=(function,),
    ).run(journal=JournalConfig(path=tmp_path / "journal.jsonl"))
    return owner


class Commands:
    def __init__(self):
        self.calls = []
        self.image = IMAGE
        self.project = PROJECT
        self.port = "18080"
        self.container_id = "c" * 64
        self.started_at = "2026-09-13T10:00:00.000000000Z"

    def __call__(self, argv, cwd, env):
        self.calls.append(argv)
        if argv[-3:] == ("ps", "-q", "control-plane"):
            return (self.container_id + "\n").encode()
        if argv[1] == "inspect":
            return json.dumps(
                [
                    {
                        "Id": self.container_id,
                        "Config": {
                            "Image": self.image,
                            "Labels": {
                                "com.docker.compose.project": self.project,
                                "com.docker.compose.service": "control-plane",
                            },
                        },
                        "State": {"Running": True, "StartedAt": self.started_at},
                        "NetworkSettings": {
                            "Ports": {
                                "8080/tcp": [
                                    {"HostIp": "127.0.0.1", "HostPort": self.port},
                                ]
                            }
                        },
                    }
                ]
            ).encode()
        assert "down" in argv
        return b""


def execute(tmp_path, command, delete):
    Workflow(workflow_id="soak-teardown").add(
        TeardownSoakTask(tmp_path, command=command, delete_function=delete),
    ).run()


def test_releases_only_owned_resources_and_replay_is_idempotent(tmp_path):
    owner = retained(tmp_path)
    command = Commands()
    deleted = []
    execute(tmp_path, command, deleted.append)
    assert deleted == [owner]
    assert sum("down" in call for call in command.calls) == 1
    assert not any("--remove-orphans" in call for call in command.calls)
    assert all(PROJECT in call for call in command.calls if "compose" in call)
    calls = list(command.calls)
    execute(tmp_path, command, deleted.append)
    assert command.calls == calls
    assert deleted == [owner]


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("image", "another@sha256:" + "d" * 64),
        ("project", "unrelated"),
        ("port", "18082"),
        ("container_id", "d" * 64),
        ("started_at", "2026-09-13T11:00:00.000000000Z"),
    ],
)
def test_wrong_live_platform_is_not_touched(tmp_path, attribute, value):
    retained(tmp_path)
    command = Commands()
    setattr(command, attribute, value)
    deleted = []
    with pytest.raises(ValueError, match=r"control.plane|retained API endpoint"):
        execute(tmp_path, command, deleted.append)
    assert deleted == []
    assert not any("down" in call for call in command.calls)


def test_unknown_resource_refused_before_any_commands(tmp_path):
    retained(tmp_path)
    journal = tmp_path / "cleanup.jsonl"
    existing = json.loads(journal.read_text().splitlines()[0])
    with journal.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "schema_version": 3,
                    "kind": "retained",
                    "resource": "unknown",
                    "order": 50,
                    "value": None,
                    "workflow_id": existing["workflow_id"],
                    "run_id": existing["run_id"],
                }
            )
            + "\n"
        )
    command = Commands()
    with pytest.raises(ValueError, match="invalid owned function record fields"):
        execute(tmp_path, command, lambda owner: pytest.fail("unexpected deletion"))
    assert command.calls == []


def test_changed_compose_file_refused_before_any_commands(tmp_path):
    retained(tmp_path)
    (tmp_path / "compose.yaml").write_text("services: {}\nvolumes: {unrelated: {}}\n")
    command = Commands()
    with pytest.raises(ValueError, match=r"owned Compose file changed since"):
        execute(tmp_path, command, lambda owner: pytest.fail("unexpected deletion"))
    assert command.calls == []


def test_cleanup_failure_keeps_platform_and_allows_retry(tmp_path):
    owner = retained(tmp_path)
    command = Commands()

    def failed_delete(identity):
        raise RuntimeError("function cleanup failed")

    with pytest.raises(RuntimeError, match="releasing retained resources failed"):
        execute(tmp_path, command, failed_delete)
    assert not any("down" in call for call in command.calls)
    deleted = []
    execute(tmp_path, command, deleted.append)
    assert deleted == [owner]
    assert sum("down" in call for call in command.calls) == 1


def test_constructor_does_not_read_journal_or_create_run_dir(tmp_path):
    root = tmp_path / "not-created"
    config = SimpleNamespace(
        workflow="soak",
        soak=SimpleNamespace(
            cancellation_timeout_s=5,
            artifact_limit_bytes=1024 * 1024,
        ),
    )
    workflow = build_teardown_workflow(
        config,
        SimpleNamespace(provider="local"),
        None,
        run_dir=root,
        repo_root=tmp_path,
        tool_root=tmp_path,
    )
    assert workflow.compile().tasks
    assert not root.exists()


def test_teardown_with_its_own_journal_preserves_retained_history(tmp_path):
    owner = retained(tmp_path)
    command = Commands()
    deleted = []
    Workflow(workflow_id="soak-teardown").add(
        TeardownSoakTask(tmp_path, command=command, delete_function=deleted.append),
    ).run(journal=JournalConfig(path=tmp_path / "journal.jsonl"))
    assert deleted == [owner]


def test_historical_title_reuse_rejected_before_commands(tmp_path):
    retained(tmp_path)
    journal = tmp_path / "cleanup.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    function = next(
        row
        for row in rows
        if row.get("kind") == "retained" and row["value"].get("name") == "fn"
    )
    release = {
        key: function[key]
        for key in ("schema_version", "workflow_id", "run_id", "resource")
    }
    release["kind"] = "released"
    with journal.open("a") as stream:
        stream.write(json.dumps(release) + "\n")
        stream.write(json.dumps(function) + "\n")
    command = Commands()
    with pytest.raises(ValueError, match="historical resource title re-retention i"):
        execute(tmp_path, command, lambda owner: pytest.fail("unexpected deletion"))
    assert command.calls == []


def test_unknown_workflow_resource_is_not_hidden_by_cleanup_journal(tmp_path):
    retained(tmp_path)
    journal = tmp_path / "journal.jsonl"
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    original = next(item for item in records if item.get("kind") == "retained")
    with journal.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    **original,
                    "resource": "unknown endpoint lease",
                    "order": 999,
                    "value": None,
                }
            )
            + "\n"
        )
    command = Commands()
    with pytest.raises(ValueError, match=r"workflow journal contains untracked"):
        execute(tmp_path, command, lambda owner: pytest.fail("unexpected delete"))
    assert command.calls == []


def test_legacy_journal_location_reconstructs_without_live_objects(tmp_path):
    owner = retained(tmp_path)
    (tmp_path / "cleanup.jsonl").unlink()
    deleted = []
    execute(tmp_path, Commands(), deleted.append)
    assert deleted == [owner]


def test_cleanup_waits_past_the_container_stop_grace(tmp_path):
    """`compose down` cannot finish before every container's stop grace elapses.

    Teardown used the cancellation budget verbatim, so the command timed out
    while Docker was still stopping and the run reported cleanup unconfirmed,
    leaving the platform up.
    """
    from nanolab.tasks.soak.teardown import cleanup_timeout_s

    assert cleanup_timeout_s(5) > 5
    assert cleanup_timeout_s(30) > 30
    # Monotonic in the declared grace, so a longer grace never gets less room.
    assert cleanup_timeout_s(30) > cleanup_timeout_s(5)
