"""Generated Compose and retry ownership must survive fresh reconstruction."""

import json

import pytest
from sonata_engine import (
    JournalConfig,
    Resource,
    Task,
    TaskOutcome,
    Workflow,
    release_retained,
)

from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.soak.retention import (
    CleanupState,
    compose_file_fingerprint,
    journaled_compose_resource,
)


class Consume(Task):
    title = "Consume owned project"

    def run(self, inputs):
        return TaskOutcome(value=None)


def resource(tmp_path, events, state, name="owned-soak"):
    path = tmp_path / "compose.yaml"
    if not path.exists():
        path.write_text("services: {}\n")
    project = DockerComposeProject(name, path, "http://127.0.0.1:18081", build=False)

    def acquire(inputs):
        events.append("acquire")
        return project

    def release(inputs, value):
        assert value == project
        events.append("release")

    return journaled_compose_resource(
        Resource(title="Own frozen Compose", acquire=acquire, release=release),
        project=project,
        cwd=tmp_path,
        cleanup_state=state,
        command=lambda *args: events.append("release") or b"",
    )


def test_fresh_adapter_replays_retained_project(tmp_path):
    events = []
    journal = JournalConfig(path=tmp_path / "cleanup.jsonl")
    original = resource(tmp_path, events, CleanupState(journal))
    Workflow(workflow_id="owned", keep=True).add(Consume(), requires=(original,)).run()
    assert events == ["acquire"]
    fresh = resource(tmp_path, events, CleanupState.restore(journal))
    assert release_retained({fresh.title: fresh}, journal) == ("Own frozen Compose",)
    assert events == ["acquire", "release"]
    assert release_retained({fresh.title: fresh}, journal) == ()


def test_replay_rejects_different_project_before_release(tmp_path):
    events = []
    journal = JournalConfig(path=tmp_path / "cleanup.jsonl")
    original = resource(tmp_path, events, CleanupState(journal))
    Workflow(workflow_id="owned", keep=True).add(Consume(), requires=(original,)).run()
    unrelated = resource(
        tmp_path, events, CleanupState.restore(journal), name="unrelated"
    )
    with pytest.raises(ValueError, match="retained Compose identity differs from o"):
        release_retained({unrelated.title: unrelated}, journal)
    assert events == ["acquire"]


@pytest.mark.parametrize(
    "body",
    [
        "services: {}\nvolumes:\n  state:\n    name: ${FOREIGN_VOLUME}\n",
        "include: other.yaml\nservices: {}\n",
        "services:\n  cp:\n    extends: {file: other.yaml, service: cp}\n",
        "services: {}\nvolumes: {state: {name: foreign}}\n",
        "services: {}\nvolumes: {state: {external: true}}\n",
        "services: {}\nnetworks: {default: {name: foreign}}\n",
        "services:\n  cp:\n    image: cp\n    env_file: .env\n",
        "services:\n  cp:\n    image: cp\n    environment: {INHERITED: null}\n",
        'services:\n  cp:\n    image: cp\n    volumes: ["/unrelated:/data"]\n',
        "services: {}\nservices: {}\n",
        "services: &services {}\nvolumes: *services\n",
    ],
)
def test_unsafe_generated_config_rejected_before_acquisition(tmp_path, body):
    (tmp_path / "compose.yaml").write_text(body)
    events = []
    owned = resource(
        tmp_path, events, CleanupState(JournalConfig(path=tmp_path / "cleanup.jsonl"))
    )
    with pytest.raises(ValueError, match=r"Compose|string keys"):
        Workflow(workflow_id="unsafe").add(Consume(), requires=(owned,)).run()
    assert events == []


def test_config_drift_blocks_release_and_preserves_retry_record(tmp_path):
    events = []
    journal = JournalConfig(path=tmp_path / "cleanup.jsonl")
    owned = resource(tmp_path, events, CleanupState(journal))
    Workflow(workflow_id="owned", keep=True).add(Consume(), requires=(owned,)).run()
    (tmp_path / "compose.yaml").write_text("services: {}\nvolumes: {new: {}}\n")
    fresh = resource(tmp_path, events, CleanupState.restore(journal))
    with pytest.raises(ValueError, match=r"owned Compose file changed since"):
        release_retained({fresh.title: fresh}, journal)
    assert events == ["acquire"]
    assert fresh.title in CleanupState.restore(journal).records


def test_acquisition_cannot_overwrite_existing_ownership(tmp_path):
    events = []
    journal = JournalConfig(path=tmp_path / "cleanup.jsonl")
    first = resource(tmp_path, events, CleanupState(journal))
    Workflow(workflow_id="owned", keep=True).add(Consume(), requires=(first,)).run()
    second = resource(tmp_path, events, CleanupState(journal))
    with pytest.raises(FileExistsError, match="Errno 17"):
        Workflow(workflow_id="other").add(Consume(), requires=(second,)).run()
    assert events == ["acquire"]


def diagnostic_compose():
    return {
        "services": {
            "cp": {
                "image": "owned@sha256:" + "a" * 64,
                "user": "65532:65532",
                "volumes": [
                    {
                        "type": "volume",
                        "source": "diagnostic-tmp-0",
                        "target": "/tmp",
                        "volume": {"nocopy": True},
                    }
                ],
            }
        },
        "volumes": {
            "diagnostic-tmp-0": {
                "driver": "local",
                "driver_opts": {
                    "type": "tmpfs",
                    "device": "tmpfs",
                    "o": (
                        "size=419430400,uid=65532,gid=65532,"
                        "mode=1777,noexec,nosuid,nodev"
                    ),
                },
                "labels": {
                    "nanolab.run": "owned-soak",
                    "nanolab.diagnostic.tmp": "true",
                },
            }
        },
    }


def test_exact_owned_local_diagnostic_tmpfs_is_replayable(tmp_path):
    path = tmp_path / "compose.yaml"
    path.write_text(json.dumps(diagnostic_compose()))
    events = []
    journal = JournalConfig(path=tmp_path / "cleanup.jsonl")
    first = resource(tmp_path, events, CleanupState(journal))
    Workflow(workflow_id="owned", keep=True).add(Consume(), requires=(first,)).run()
    fresh = resource(tmp_path, events, CleanupState.restore(journal))
    release_retained({fresh.title: fresh}, journal)
    assert events == ["acquire", "release"]


@pytest.mark.parametrize(
    "mutation",
    [
        "external",
        "name",
        "owner",
        "driver",
        "bind-device",
        "percentage",
        "duplicate",
        "unaligned",
        "oversized",
        "missing-flag",
        "wrong-user",
        "wrong-target",
        "anonymous",
        "copy",
        "unmounted",
    ],
)
def test_diagnostic_volume_whitelist_does_not_admit_foreign_or_unbounded_resources(
    tmp_path, mutation
):
    document = diagnostic_compose()
    volume = document["volumes"]["diagnostic-tmp-0"]
    service = document["services"]["cp"]
    if mutation in {"external", "name"}:
        volume[mutation] = True if mutation == "external" else "foreign"
    elif mutation == "owner":
        volume["labels"]["nanolab.run"] = "foreign"
    elif mutation == "driver":
        volume["driver"] = "plugin"
    elif mutation == "bind-device":
        volume["driver_opts"].update({"type": "none", "device": "/", "o": "bind"})
    elif mutation in {"percentage", "unaligned", "oversized"}:
        size = {
            "percentage": "50%",
            "unaligned": "4097",
            "oversized": str(2 * 1024**3),
        }[mutation]
        volume["driver_opts"]["o"] = volume["driver_opts"]["o"].replace(
            "419430400", size
        )
    elif mutation == "duplicate":
        volume["driver_opts"]["o"] += ",size=4096"
    elif mutation == "missing-flag":
        volume["driver_opts"]["o"] = volume["driver_opts"]["o"].replace(",noexec", "")
    elif mutation == "wrong-user":
        service["user"] = "0:0"
    elif mutation == "wrong-target":
        service["volumes"][0]["target"] = "/foreign"
    elif mutation == "anonymous":
        service["volumes"][0]["source"] = "foreign"
    elif mutation == "copy":
        service["volumes"][0]["volume"] = {"nocopy": False}
    elif mutation == "unmounted":
        service["volumes"] = []
    path = tmp_path / "compose.yaml"
    path.write_text(json.dumps(document))
    project = DockerComposeProject(
        "owned-soak", path, "http://127.0.0.1:18081", build=False
    )
    with pytest.raises(
        ValueError,
        match=(
            r"generated Compose diagnostic|generated Compose permits|"
            r"generated Compose tmpfs|undeclared generated Compose|"
            r"unsupported generated Compose"
        ),
    ):
        compose_file_fingerprint(project, tmp_path)


def test_diagnostic_volume_config_drift_preserves_retry_record(tmp_path):
    document = diagnostic_compose()
    path = tmp_path / "compose.yaml"
    path.write_text(json.dumps(document))
    events = []
    journal = JournalConfig(path=tmp_path / "cleanup.jsonl")
    first = resource(tmp_path, events, CleanupState(journal))
    Workflow(workflow_id="owned", keep=True).add(Consume(), requires=(first,)).run()
    document["volumes"]["diagnostic-tmp-0"]["driver_opts"]["o"] = document["volumes"][
        "diagnostic-tmp-0"
    ]["driver_opts"]["o"].replace("419430400", "4096")
    path.write_text(json.dumps(document))
    fresh = resource(tmp_path, events, CleanupState.restore(journal))
    with pytest.raises(ValueError, match=r"owned Compose file changed since"):
        release_retained({fresh.title: fresh}, journal)
    assert events == ["acquire"]
    assert fresh.title in CleanupState.restore(journal).records
