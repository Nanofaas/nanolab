"""Optional collector lifecycle must preserve legacy runs and external resources."""

from typing import cast

import pytest
from pydantic import ValidationError

from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot.infrastructure import NodeResource


def settings(logs=None):
    data = {
        "provider": "multipass",
        "purpose": "workflow-validation",
        "nodes": [
            {"id": "edge-0", "kind": "edge"},
            {"id": "edge-1", "kind": "edge"},
            {"id": "cloud", "kind": "cloud"},
        ],
        "functions": {"f": {"input": {}, "cpu": 0.5, "maxReplicas": 2}},
    }
    if logs is not None:
        data["observability"] = {"logs": logs}
    return OneShotConfig.model_validate(data)


def test_disabled_preserves_legacy_budget_and_fingerprint():
    assert not settings().observability.logs.enabled


def test_enabled_reserves_collector_and_rejects_missing_endpoint():
    with pytest.raises(ValidationError, match="endpoint"):
        settings({"enabled": True})
    config = settings(
        {"enabled": True, "endpoint": "http://192.168.64.1:3100/loki/api/v1/push"}
    )
    assert config.observability.logs.cpu == 0.1
    assert config.observability.logs.memory_mib == 64
    assert config.observability.logs.buffer_mib == 256
    fingerprint = config.observability.logs.fingerprint()
    assert fingerprint is not None and fingerprint["format"] == "json"


def test_logging_fingerprint_excludes_run_identity_and_includes_budget():
    first = settings(
        {
            "enabled": True,
            "endpoint": "http://192.168.64.1:3100/loki/api/v1/push",
            "campaignId": "first",
        }
    )
    second = settings(
        {
            "enabled": True,
            "endpoint": "http://192.168.64.1:3100/loki/api/v1/push",
            "campaignId": "second",
        }
    )
    assert (
        first.observability.logs.fingerprint()
        == second.observability.logs.fingerprint()
    )
    second.observability.logs.cpu = 0.2
    assert (
        first.observability.logs.fingerprint()
        != second.observability.logs.fingerprint()
    )


def test_collector_must_leave_room_for_compute():
    with pytest.raises(ValidationError, match="collector"):
        settings(
            {
                "enabled": True,
                "endpoint": "http://192.168.64.1:3100/loki/api/v1/push",
                "cpu": 2,
            }
        )


def test_external_backend_is_never_mutated(tmp_path, monkeypatch):
    from sonata_engine import TaskInputs

    from nanolab.tasks.one_shot.logging import backend_resource

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.logging.subprocess.run",
        lambda *args, **kwargs: pytest.fail("external backend mutation"),
    )
    logs = settings(
        {"enabled": True, "endpoint": "http://10.0.0.1:3100/loki/api/v1/push"}
    ).observability.logs
    resource = backend_resource(logs, root=tmp_path, run_dir=tmp_path)
    inputs = TaskInputs._for_resources({}, set())
    endpoint = resource.acquire(inputs)
    resource.release(inputs, endpoint)
    assert endpoint == logs.endpoint


def fake_collector(
    tmp_path,
    monkeypatch,
    *,
    running=True,
    export_error=False,
    metrics="",
    archive_error=False,
):
    import hashlib
    import json
    from types import SimpleNamespace

    from sonata_engine import Resource, TaskInputs

    from nanolab.tasks.one_shot.logging import collector_resource

    root = tmp_path / "root"
    assets = root / "deploy/observability"
    assets.mkdir(parents=True)
    for filename in ["fluent-bit.conf", "parsers.conf"]:
        (assets / filename).write_text(filename)
    commands = []

    class Node:
        provider: "Node"
        config = SimpleNamespace(id="edge-0", kind="edge")
        request = object()
        vm = Resource(
            title="vm",
            acquire=lambda _: SimpleNamespace(home="/home/ubuntu"),
            release=lambda *_: None,
        )

        def command(self, argv):
            commands.append(argv)
            if export_error and argv[0] == "python3":
                raise RuntimeError("metrics down")
            text = ""
            if argv[:2] == ("mktemp", "-d"):
                text = "/home/ubuntu/private"
            if argv[0] == "python3":
                text = metrics
            if argv[:3] == ("docker", "volume", "inspect"):
                text = "/var/lib/docker/volumes/owned/_data"
            if argv[:2] == ("sha256sum", "/home/ubuntu"):
                raise AssertionError("unexpected path")
            if argv[0] == "sha256sum":
                text = (
                    hashlib.sha256(b"recoverable-state").hexdigest() + "  " + argv[1]
                    if argv[1].endswith("state.tar.gz")
                    else "\n".join(
                        hashlib.sha256(
                            (assets / Path(value).name).read_bytes()
                        ).hexdigest()
                        + "  "
                        + value
                        for value in argv[1:]
                    )
                )
            if argv[:2] == ("docker", "info"):
                text = "json-file" if "LoggingDriver" in argv[-1] else "/var/lib/docker"
            if argv[:2] == ("docker", "inspect"):
                text = json.dumps(
                    [
                        {
                            "HostConfig": {
                                "NanoCpus": 100000000,
                                "Memory": 64 * 1024 * 1024,
                            },
                            "Image": "sha256:known",
                            "State": {"Running": running},
                        }
                    ]
                )
            return SimpleNamespace(stdout=text, stderr="", return_code=0)

        def exec_argv(self, request, argv):
            commands.append(argv)
            if argv[:3] == ("docker", "volume", "inspect"):
                return SimpleNamespace(return_code=1, stdout="", stderr="not found")
            return SimpleNamespace(return_code=0, stdout="", stderr="")

        def transfer_to(self, *args, **kwargs):
            return SimpleNamespace(return_code=0)

        def transfer_from(self, *args, **kwargs):
            commands.append(("archive-transfer",))
            if archive_error:
                raise OSError("archive disk full")
            kwargs["destination"].write_bytes(b"recoverable-state")
            return SimpleNamespace(return_code=0)

    from pathlib import Path

    node = Node()
    node.provider = node
    bootstrap = Resource(title="boot", acquire=lambda _: None, release=lambda *_: None)
    backend = Resource(
        title="external",
        acquire=lambda _: "http://10.0.0.1:3100/loki/api/v1/push",
        release=lambda *_: None,
    )
    logs = settings(
        {"enabled": True, "endpoint": "http://10.0.0.1:3100/loki/api/v1/push"}
    ).observability.logs
    resource = collector_resource(
        cast(NodeResource, node),
        logs,
        bootstrap=bootstrap,
        backend=backend,
        root=root,
        run_dir=tmp_path / "run",
        mode="BASELINE",
    )
    monkeypatch.setattr("nanolab.tasks.one_shot.logging.time.sleep", lambda _: None)

    inputs = TaskInputs._for_resources(
        {
            node.vm: SimpleNamespace(home="/home/ubuntu"),
            backend: logs.endpoint,
            bootstrap: None,
        },
        {node.vm, backend, bootstrap},
    )
    return resource, inputs, commands


def successful_query(monkeypatch):
    import httpx

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.logging.httpx.Client.get",
        lambda *args, **kwargs: httpx.Response(
            200,
            request=httpx.Request("GET", "http://loki.test"),
            json={"data": {"result": [{"values": [["1", "record"]]}]}},
        ),
    )


@pytest.mark.parametrize(
    "metrics",
    [
        "",
        'fluentbit_input_records_total{name="tail.0"} 3\n'
        'fluentbit_output_proc_records_total{name="loki.0"} 1\n'
        'fluentbit_output_dropped_records_total{name="loki.0"} 1\n',
        'fluentbit_input_records_total{name="tail.0"} 3\n'
        'fluentbit_output_proc_records_total{name="loki.0"} 1\n'
        'fluentbit_output_dropped_records_total{name="loki.0"} 0\n',
    ],
)
def test_successful_query_is_not_proof_of_complete_collection(
    tmp_path, monkeypatch, metrics
):
    import json

    resource, inputs, commands = fake_collector(tmp_path, monkeypatch, metrics=metrics)
    successful_query(monkeypatch)
    value = resource.acquire(inputs)
    resource.release(inputs, value)
    directory = tmp_path / "run/logging/edge-0"
    status = json.loads((directory / "status.json").read_text())
    assert status["status"] == "partial"
    assert status["exportStatus"] == "complete"
    assert (directory / "collector-state.tar.gz").read_bytes() == b"recoverable-state"
    archive_at = commands.index(("archive-transfer",))
    assert archive_at < next(
        i for i, argv in enumerate(commands) if argv[:3] == ("docker", "volume", "rm")
    )


def test_existing_metrics_endpoint_cannot_hide_a_dead_owned_agent(
    tmp_path, monkeypatch
):
    resource, inputs, _ = fake_collector(tmp_path, monkeypatch, running=False)
    with pytest.raises(RuntimeError, match=r"collector.*running"):
        resource.acquire(inputs)


def test_export_failure_is_partial_and_owned_cleanup_still_runs(tmp_path, monkeypatch):
    import json

    import httpx

    resource, inputs, commands = fake_collector(
        tmp_path, monkeypatch, export_error=True
    )

    def fail(*args, **kwargs):
        raise httpx.ConnectError("backend down")

    monkeypatch.setattr("nanolab.tasks.one_shot.logging.httpx.Client.get", fail)
    value = resource.acquire(inputs)
    resource.release(inputs, value)
    status = json.loads((tmp_path / "run/logging/edge-0/status.json").read_text())
    assert status["status"] == "partial"
    assert (tmp_path / "run/logging/edge-0/collector-state.tar.gz").exists()
    assert any(argv[:3] == ("docker", "rm", "-f") for argv in commands)
    assert any(argv[:3] == ("docker", "volume", "rm") for argv in commands)
    assert not any(argv[:2] == ("docker", "compose") for argv in commands)


def test_drained_collection_requires_delivery_counters_and_verified_archive(
    tmp_path, monkeypatch
):
    import json

    metrics = (
        'fluentbit_input_records_total{name="tail.0"} 1\n'
        'fluentbit_output_proc_records_total{name="loki.0"} 1\n'
        'fluentbit_output_dropped_records_total{name="loki.0"} 0\n'
    )
    resource, inputs, commands = fake_collector(tmp_path, monkeypatch, metrics=metrics)
    successful_query(monkeypatch)
    value = resource.acquire(inputs)
    resource.release(inputs, value)
    status = json.loads((tmp_path / "run/logging/edge-0/status.json").read_text())
    assert status["status"] == "complete"
    assert status["collection"]["status"] == "drained"
    assert status["stateArchive"] == "verified"
    stop = next(i for i, argv in enumerate(commands) if argv[:2] == ("docker", "stop"))
    assert stop < commands.index(("archive-transfer",))


def test_measured_collector_config_is_in_profile_fingerprint():
    from types import SimpleNamespace

    from nanolab.tasks.one_shot.calibration import environment_identity

    config = settings(
        {"enabled": True, "endpoint": "http://10.0.0.1:3100/loki/api/v1/push"}
    )
    from datetime import UTC, datetime

    from nanolab.tasks.one_shot.preflight import TopologyEvidence

    topology = TopologyEvidence(
        measured_at=datetime.now(UTC),
        generator_location="operator-host",
        nodes=[],
        logging={
            node.id: {
                "image": "sha256:known",
                "configSha256": {"fluent-bit.conf": "a" * 64},
            }
            for node in config.nodes
        },
    )
    built = SimpleNamespace(
        source={"revision": "a" * 40}, recipe_sha256="b" * 64, components=[]
    )
    first = environment_identity(config, topology, built)[0]
    config.observability.logs.campaign_id = "new-run"
    assert environment_identity(config, topology, built)[0] == first
    assert topology.logging is not None
    topology.logging["edge-0"]["configSha256"]["fluent-bit.conf"] = "c" * 64
    assert environment_identity(config, topology, built)[0] != first
    topology.logging = None
    with pytest.raises(ValueError, match="collector identities"):
        environment_identity(config, topology, built)


@pytest.mark.parametrize("capacity", [(7, 100000), (20, 12500)])
def test_owned_backend_is_reserved_in_host_budget(monkeypatch, capacity):
    from nanolab.config import EnvironmentConfig
    from nanolab.one_shot.infrastructure import build_one_shot_resources

    config = settings(
        {
            "enabled": True,
            "endpoint": "http://10.0.0.1:3100/loki/api/v1/push",
            "backendOwned": True,
            "backendBind": "10.0.0.1",
        }
    )
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "logging"}}}
    )
    monkeypatch.setattr(
        "nanolab.one_shot.infrastructure.host_capacity", lambda: capacity
    )
    with pytest.raises(ValueError, match="backend"):
        build_one_shot_resources(config, environment)


def test_export_disk_error_does_not_skip_owned_cleanup(tmp_path, monkeypatch):
    from pathlib import Path

    resource, inputs, commands = fake_collector(
        tmp_path, monkeypatch, archive_error=True
    )
    value = resource.acquire(inputs)

    def disk_full(*args, **kwargs):
        raise OSError("export disk full")

    monkeypatch.setattr(Path, "write_text", disk_full)
    with pytest.raises(RuntimeError, match="state archive"):
        resource.release(inputs, value)
    assert any(argv[:3] == ("docker", "rm", "-f") for argv in commands)
    assert any(argv[:3] == ("docker", "volume", "rm") for argv in commands)


def test_compensation_does_not_repeat_completed_cleanup_after_archive_failure(
    tmp_path, monkeypatch
):
    resource, inputs, commands = fake_collector(
        tmp_path, monkeypatch, archive_error=True
    )
    successful_query(monkeypatch)
    value = resource.acquire(inputs)
    with pytest.raises(RuntimeError, match="state archive"):
        resource.release(inputs, value)
    count = len(commands)
    resource.release(inputs, value)
    assert len(commands) == count


def test_backend_export_disk_error_does_not_skip_owned_cleanup(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from sonata_engine import TaskInputs

    from nanolab.tasks.one_shot.logging import backend_resource

    logs = settings(
        {
            "enabled": True,
            "endpoint": "http://10.0.0.1:3100/loki/api/v1/push",
            "backendOwned": True,
            "backendBind": "10.0.0.1",
        }
    ).observability.logs
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def disk_full(*args, **kwargs):
        raise OSError("export disk full")

    monkeypatch.setattr("nanolab.tasks.one_shot.logging.subprocess.run", run)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.logging.subprocess.check_output",
        lambda *args, **kwargs: "",
    )
    monkeypatch.setattr(Path, "write_text", disk_full)
    resource = backend_resource(logs, root=tmp_path, run_dir=tmp_path)
    assert logs.endpoint is not None
    resource.release(TaskInputs._for_resources({}, set()), logs.endpoint)
    assert any("down" in argv for argv in commands)


def test_backend_rejects_unconfigured_endpoint_even_outside_scenario(tmp_path):
    from nanolab.tasks.one_shot.logging import backend_resource

    with pytest.raises(ValueError, match="endpoint"):
        backend_resource(settings().observability.logs, root=tmp_path, run_dir=tmp_path)


@pytest.mark.parametrize(
    "image",
    ["registry.example:5000/fluent-bit", "registry.example:5000/fluent-bit:latest"],
)
def test_registry_port_does_not_pin_an_image(image):
    with pytest.raises(ValidationError, match="pinned"):
        settings(
            {
                "enabled": True,
                "endpoint": "http://10.0.0.1:3100/loki/api/v1/push",
                "image": image,
            }
        )


@pytest.mark.parametrize("bind", ["0.0.0.0", "8.8.8.8"])
def test_owned_backend_requires_private_vm_facing_binding(bind):
    with pytest.raises(ValidationError, match="private"):
        settings(
            {
                "enabled": True,
                "endpoint": "http://10.0.0.1:3100/loki/api/v1/push",
                "backendOwned": True,
                "backendBind": bind,
            }
        )
