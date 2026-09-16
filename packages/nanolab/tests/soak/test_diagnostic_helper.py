"""Synthetic helper boundaries; no Docker daemon or runtime is contacted."""

import importlib.util
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from nanolab.tasks.soak.diagnostic_helper import (
    DockerHelperSpec,
    helper_create_argv,
    validate_container,
    validate_probe,
)
from nanolab.tasks.soak.models import Target

CID = "a" * 64
DIGEST = "example/target@sha256:" + "b" * 64
HELPER = "example/helper@sha256:" + "c" * 64
TARGET = Target("control-plane", CID, 1234, "2026-09-13T00:00:00Z", DIGEST, "jvm")


def spec(tmp_path, **kwargs):
    values = {
        "target": TARGET,
        "helper_image": HELPER,
        "owner_label": "nanolab.run",
        "owner_value": "run-123",
        "uid": 1000,
        "gid": 1000,
        "output_root": tmp_path,
        "quota_bytes": 16 * 1024 * 1024,
        "helper_memory_bytes": 128 * 1024 * 1024,
        "allow_target_stop_on_cancel": True,
        "target_tmp_volume": "owned-tmp",
    }
    values.update(kwargs)
    return DockerHelperSpec(**values)


def container():
    return {
        "Id": CID,
        "Image": "sha256:" + "d" * 64,
        "Mounts": [
            {
                "Type": "volume",
                "Name": "owned-tmp",
                "Driver": "local",
                "Destination": "/tmp",
                "Source": "/docker/owned-tmp/_data",
                "RW": True,
            }
        ],
        "Config": {"Labels": {"nanolab.run": "run-123"}},
        "State": {"Pid": 1234, "Running": True, "StartedAt": TARGET.process_started_at},
        "HostConfig": {
            "Privileged": False,
            "PidMode": "",
            "NetworkMode": "bridge",
            "CapAdd": None,
        },
    }


def probe():
    return {
        "runtime": "jvm",
        "version": "25.0.1",
        "helper_version": "25.0.1",
        "uid": 1000,
        "gid": 1000,
        "target_uid": 1000,
        "target_gid": 1000,
        "target_pid": 1,
        "target_start_ticks": "314",
        "pid_namespace": "pid:[4026533000]",
        "target_pid_namespace": "pid:[4026533000]",
        "mount_namespace": "mnt:[4026533001]",
        "target_mount_namespace": "mnt:[4026533002]",
        "quota": {
            "type": "tmpfs",
            "capacity_bytes": 16 * 1024 * 1024,
            "target_write_bytes": 8192,
        },
        "attach_output": "OpenJDK 64-Bit Server VM version 25.0.1",
        "helper_pid": 8,
        "private_control": False,
    }


def test_helper_container_is_restricted_and_target_bound(tmp_path):
    argv = helper_create_argv(spec(tmp_path), "owned-helper")
    assert argv[argv.index("--pid") + 1] == "container:" + CID
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert (
        argv[argv.index("--tmpfs") + 1]
        == "/out:rw,noexec,nosuid,nodev,size=16777216,mode=0700,uid=1000,gid=1000"
    )
    assert "--privileged" not in argv and "--cap-add" not in argv
    assert HELPER in argv and "--pull=never" in argv


@pytest.mark.parametrize(
    "changes",
    [
        {"helper_image": "helper:latest"},
        {"allow_target_stop_on_cancel": False},
        {"uid": -1},
        {"quota_bytes": 0},
        {"owner_value": ""},
        {"target": replace(TARGET, container_id="friendly-name")},
        {"target": replace(TARGET, runtime="native")},
    ],
)
def test_provisioning_rejects_unsafe_or_unbounded_configuration(tmp_path, changes):
    with pytest.raises(
        ValueError,
        match=(
            r"exact owned container-init identity|matching numeric UID|"
            r"owner label and explicit target-stop|page-aligned quota and helper memory"
        ),
    ):
        spec(tmp_path, **changes)


def test_container_requires_exact_owner_incarnation_and_image(tmp_path):
    cfg = spec(tmp_path)
    validate_container(cfg, container(), {"RepoDigests": [DIGEST]})
    for field, value in [("Pid", 1235), ("StartedAt", "different"), ("Running", False)]:
        data = container()
        data["State"][field] = value
        with pytest.raises(ValueError, match=r"owned target identity or isolation"):
            validate_container(cfg, data, {"RepoDigests": [DIGEST]})
    data = container()
    data["Config"]["Labels"] = {}
    with pytest.raises(ValueError, match=r"owned target identity or isolation"):
        validate_container(cfg, data, {"RepoDigests": [DIGEST]})
    with pytest.raises(ValueError, match=r"owned target identity or isolation"):
        validate_container(cfg, container(), {"RepoDigests": []})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_uid", 0),
        ("target_pid_namespace", "pid:[other]"),
        ("helper_version", "21.0.1"),
        ("version", "21.0.1"),
        ("attach_output", ""),
        ("target_start_ticks", ""),
    ],
)
def test_probe_rejects_unobserved_attach_identity_and_compatibility(
    tmp_path, field, value
):
    data = probe()
    data[field] = value
    with pytest.raises(
        ValueError, match=r"compatible JVM\d+ target|unverified runtime attachment"
    ):
        validate_probe(spec(tmp_path), data)


@pytest.mark.parametrize(
    "quota",
    [
        {"type": "overlay", "capacity_bytes": 16777216, "target_write_bytes": 8192},
        {"type": "tmpfs", "capacity_bytes": 33554432, "target_write_bytes": 8192},
        {"type": "tmpfs", "capacity_bytes": 16777216, "target_write_bytes": 0},
    ],
)
def test_quota_requires_kernel_bound_and_actual_target_write(tmp_path, quota):
    data = probe()
    data["quota"] = quota
    with pytest.raises(ValueError, match=r"kernel quota and actual target-side"):
        validate_probe(spec(tmp_path), data)


def test_probe_accepts_observed_compatible_attachment(tmp_path):
    assert validate_probe(spec(tmp_path), probe()) == 16777216


def worker():
    path = Path(__file__).parents[2] / "assets/soak/diagnostic-worker.py"
    module_spec = importlib.util.spec_from_file_location("diagnostic_worker", path)
    module = importlib.util.module_from_spec(module_spec)  # pyright: ignore[reportArgumentType]
    module_spec.loader.exec_module(module)  # pyright: ignore[reportOptionalMemberAccess]
    return module


def test_jfr_gc_evidence_requires_full_collection_cause_and_window():
    module = worker()
    event = {
        "type": "jdk.GarbageCollection",
        "values": {
            "name": "G1Full",
            "cause": "Diagnostic Command",
            "gcId": 7,
            "startTime": "2026-09-13T00:00:01Z",
            "duration": "PT0.01S",
        },
    }
    import datetime

    start = datetime.datetime(2026, 9, 13, tzinfo=datetime.UTC).timestamp()
    body = {"recording": {"events": [event]}}
    assert module.full_gc_events(body, start, start + 2) == [event]
    for name, cause in [
        ("G1New", "Diagnostic Command"),
        ("G1Full", "Allocation Failure"),
    ]:
        altered = json.loads(json.dumps(body))
        altered["recording"]["events"][0]["values"].update(name=name, cause=cause)
        with pytest.raises(ValueError, match=r"no request-window JFR full GC caused"):
            module.full_gc_events(altered, start, start + 2)
    with pytest.raises(ValueError, match=r"no request-window JFR full GC caused"):
        module.full_gc_events(body, start + 2, start + 3)
    with pytest.raises(ValueError, match=r"no request-window JFR full GC caused"):
        module.full_gc_events({"recording": {"events": []}}, start, start + 2)


def test_remote_request_ignores_untrusted_arbitrary_command(tmp_path):
    module = worker()
    message = {
        "kind": "execute",
        "operation": "shell",
        "argv": ["/bin/sh"],
        "target": asdict(TARGET),
        "request_id": "req",
        "max_bytes": 4096,
        "timeout_s": 2,
        "started_s": 1,
    }
    with pytest.raises(ValueError, match=r"unprovisioned or unbounded diagnostic"):
        module.validate_request(message)


def test_helper_probe_rejects_different_mount_and_node_public_inspector(tmp_path):
    node = replace(TARGET, runtime="node")
    data = probe()
    data.update(
        runtime="node",
        version="22.12.0",
        private_control=True,
        inspector_url="ws://127.0.0.1:9229/abc",
    )
    with pytest.raises(ValueError, match=r"Node control must be an in-process"):
        validate_probe(spec(tmp_path, target=node), data)


def test_request_timeout_rejects_nonfinite_or_boolean_values():
    module = worker()
    for timeout in (True, float("nan"), float("inf"), 0, -1):
        with pytest.raises(ValueError, match=r"unprovisioned or unbounded diagnostic"):
            module.validate_request(
                {
                    "kind": "execute",
                    "operation": "gc",
                    "request_id": "req",
                    "max_bytes": 4096,
                    "timeout_s": timeout,
                }
            )


def test_heapdump_command_exit_is_not_completion_evidence():
    module = worker()
    assert module.heap_dump_completed("Heap dump file created [1234 bytes in 0.1 secs]")
    assert not module.heap_dump_completed(
        "1:\nUnable to create /out/capture.hprof: No space left on device"
    )
    assert not module.heap_dump_completed("Command executed successfully")


def test_cancellation_stops_exact_target_before_removing_helper(tmp_path, monkeypatch):
    from nanolab.tasks.soak import diagnostic_helper as helper

    calls = []
    target = container()

    class Commands:
        cleanup_deadline = None

        def inspect(self, identity, **kwargs):
            if identity == CID:
                return target
            assert identity == "e" * 64
            return {
                "Id": identity,
                "Config": {
                    "Labels": {
                        "nanolab.run": "run-123",
                        "nanolab.diagnostic.target": CID,
                    }
                },
            }

        def run(self, args, **kwargs):
            calls.append(args)
            if args[0] == "kill":
                target["State"]["Running"] = False
            return ""

    executor = object.__new__(helper.LocalDockerDiagnosticExecutor)
    executor.spec, executor.commands = spec(tmp_path), Commands()
    executor.helper_id, executor.process_identity = "e" * 64, {"start_ticks": "314"}
    executor._closed = False
    monkeypatch.setattr(helper, "_proc_identity", lambda pid: {"start_ticks": "314"})
    executor.cancel_remote()
    assert calls == [("kill", "--signal", "KILL", CID), ("rm", "--force", "e" * 64)]
    assert executor._closed
    executor.close()
    assert len(calls) == 2


def test_cancellation_refuses_restarted_or_unowned_target(tmp_path):
    from nanolab.tasks.soak import diagnostic_helper as helper

    data = container()
    data["State"]["StartedAt"] = "new-incarnation"

    class Commands:
        cleanup_deadline = None

        def inspect(self, identity, **kwargs):
            return data

        def run(self, args, **kwargs):
            pytest.fail("must not signal a changed target")

    executor = object.__new__(helper.LocalDockerDiagnosticExecutor)
    executor.spec, executor.commands = spec(tmp_path), Commands()
    executor._closed = False
    with pytest.raises(ValueError, match="target changed"):
        executor.cancel_remote()


def test_target_restart_policy_must_allow_deterministic_cancellation(tmp_path):
    data = container()
    data["HostConfig"]["RestartPolicy"] = {"Name": "always"}
    with pytest.raises(ValueError, match=r"owned target identity or isolation"):
        validate_container(spec(tmp_path), data, {"RepoDigests": [DIGEST]})


def test_framed_bridge_executes_pinned_cli_under_existing_supervisor(tmp_path):
    import base64
    import hashlib
    import sys

    from nanolab.tasks.soak.diagnostic_exec import (
        HelperProvisioning,
        ProvisionedDiagnosticExecutor,
    )
    from nanolab.tasks.soak.diagnostics import DiagnosticRequest

    # This is a deliberately synthetic CLI, not a Docker invocation. It decodes
    # the real bridge wire request and streams a fake artifact to the real parser.
    cli = tmp_path / "synthetic-cli"
    cli.write_text(
        "#!/usr/bin/python3\n"
        + """import base64,json,sys
value=json.loads(base64.b64decode(sys.argv[-1]))
request=value['request']
if request['kind']=='inspect':
 print(json.dumps(dict(kind='result',target=request['target'],running=True)))
else:
 print(json.dumps(dict(kind='artifact',name='synthetic.txt',data_base64='eA==')))
 print(json.dumps(dict(kind='result',target=request['target'],exit_code=0,completed=True,descendants_reaped=True)))
"""
    )
    cli.chmod(0o700)
    bridge = Path(__file__).parents[2] / "assets/soak/docker-diagnostic-bridge.py"
    python = Path(sys.executable).resolve()
    cfg = {
        "docker": str(cli),
        "docker_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
        "docker_host": "unix:///unused",
        "helper_id": "e" * 64,
        "config": {"target": asdict(TARGET)},
    }
    command = (
        str(python),
        str(bridge),
        base64.b64encode(json.dumps(cfg).encode()).decode(),
    )
    receipt = {
        "schema": "nanolab-soak-diagnostic-helper-v1",
        "target": asdict(TARGET),
        "command": list(command),
        "helper_digest": HELPER,
        "operations": ["heap_dump"],
        "attach_verified": True,
        "runtime_compatible": True,
        "bounded_execution": True,
        "namespace_verified": True,
        "control_transport": "target-namespace",
        "host_output_root": str(tmp_path),
        "helper_output_root": str(tmp_path),
        "output_mode": "framed-stdout",
        "file_output_quota_bytes": 4096,
        "file_output_quota_verified": True,
        "command_artifacts": [
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in (python, bridge)
        ],
    }
    path = tmp_path / "synthetic-receipt.json"
    path.write_text(json.dumps(receipt))
    executor = ProvisionedDiagnosticExecutor(
        HelperProvisioning(path, hashlib.sha256(path.read_bytes()).hexdigest(), command)
    )
    output = tmp_path / "output"
    output.mkdir()
    outcome = executor.execute(
        DiagnosticRequest(
            "synthetic", TARGET, "heap_dump", (), None, output, 4096, 5, 1
        )
    )
    assert outcome.completed
    assert (output / "synthetic.txt").read_bytes() == b"x"


def test_prepare_observations_produce_usable_existing_adapter_without_docker(
    tmp_path, monkeypatch
):
    from nanolab.tasks.soak import diagnostic_helper as helper
    from nanolab.tasks.soak.diagnostics import DiagnosticBudget, JvmDiagnosticAdapter

    cli = tmp_path / "never-executed-cli"
    cli.write_bytes(b"synthetic identity only")
    config = spec(tmp_path, docker=str(cli))
    target_data = container()
    helper_id = "e" * 64
    image_id = "sha256:" + "f" * 64
    target_image = {
        "Id": target_data["Image"],
        "RepoDigests": [DIGEST],
        "Architecture": "arm64",
        "Os": "linux",
    }
    helper_image = {
        "Id": image_id,
        "RepoDigests": [HELPER],
        "Architecture": "arm64",
        "Os": "linux",
    }
    helper_data = {
        "Id": helper_id,
        "Image": image_id,
        "Mounts": target_data["Mounts"],
        "State": {"Pid": 5678, "Running": True},
        "HostConfig": {
            "Privileged": False,
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "NetworkMode": "none",
            "PidMode": "container:" + CID,
            "ReadonlyRootfs": True,
            "SecurityOpt": ["no-new-privileges:true"],
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "Memory": 134217728,
        },
        "Config": {
            "Labels": {"nanolab.run": "run-123", "nanolab.diagnostic.target": CID}
        },
    }
    process = {
        "uid": 1000,
        "gid": 1000,
        "ns_pid": 1,
        "start_ticks": "314",
        "pid_namespace": "pid:[4026533000]",
        "mount_namespace": "mnt:[4026533002]",
    }
    helper_process = {**process, "ns_pid": 8, "mount_namespace": "mnt:[4026533001]"}
    observed = probe()
    observed["output_alias"] = [tmp_path.stat().st_dev, tmp_path.stat().st_ino]

    def run(self, args, *positional, **kwargs):
        if args[:2] == ("volume", "inspect"):
            assert args[2] == "owned-tmp"
            return json.dumps(
                [
                    {
                        "Name": "owned-tmp",
                        "Driver": "local",
                        "Scope": "local",
                        "CreatedAt": "2026-09-13T15:00:00Z",
                        "Mountpoint": "/docker/owned-tmp/_data",
                        "Labels": {
                            "nanolab.run": "run-123",
                            "nanolab.diagnostic.tmp": "true",
                        },
                        "Options": {
                            "type": "tmpfs",
                            "device": "tmpfs",
                            "o": (
                                "size=16777216,uid=1000,gid=1000,mode=1777,"
                                "noexec,nosuid,nodev"
                            ),
                        },
                    }
                ]
            )
        if args[:2] == ("container", "inspect"):
            return json.dumps([target_data if args[2] == CID else helper_data])
        if args[:2] == ("image", "inspect"):
            return json.dumps([helper_image if args[2] == HELPER else target_image])
        if args[0] == "create":
            return helper_id
        if args[0] == "start":
            return helper_id
        if args[0] == "exec":
            return json.dumps(observed)
        raise AssertionError(args)

    monkeypatch.setattr(helper._DockerCommands, "run", run)
    monkeypatch.setattr(
        helper, "_proc_identity", lambda pid: process if pid == 1234 else helper_process
    )
    assets = Path(__file__).parents[2] / "assets/soak"
    prepared = helper.LocalDockerDiagnosticProvisioner(assets_dir=assets).prepare(
        config
    )
    assert prepared.executor.capabilities().operations == frozenset({"gc", "heap_dump"})
    receipt = json.loads(prepared.receipt.read_text())
    assert receipt["file_output_quota_bytes"] == 16777216
    assert (prepared.receipt.parent / "observations.json").is_file()
    adapter = prepared.adapter(
        budget=DiagnosticBudget(1, 16777216, 33554432),
        natural_checkpoint=tmp_path / "natural.json",
        max_capture_bytes=16777216,
    )
    assert isinstance(adapter, JvmDiagnosticAdapter)
    assert adapter.capabilities(TARGET) == frozenset({"gc", "heap_dump"})


def test_memory_only_helper_supports_native_without_target_stop_permission(tmp_path):
    cfg = spec(
        tmp_path,
        target=replace(TARGET, runtime="native"),
        memory_only=True,
        allow_target_stop_on_cancel=False,
    )
    argv = helper_create_argv(cfg, "memory-only")
    assert "container:" + CID in argv
    assert "--mount" not in argv
    assert "--cap-add" not in argv and "--privileged" not in argv


def test_memory_worker_preserves_raw_pss_and_permission_denial(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)

    def read(path, limit):
        if path.name == "status":
            return "Name:\tjava\nVmRSS:\t4096 kB\n"
        raise PermissionError("synthetic ptrace denial")

    monkeypatch.setattr(module, "read_proc", read)
    result = module.memory({"target": asdict(TARGET)})
    assert result["status"] == "Name:\tjava\nVmRSS:\t4096 kB\n"
    assert result["smaps_rollup"] is None
    assert "PermissionError" in result["errors"]["smaps_rollup"]
    assert result["target"] == asdict(TARGET)
    assert result["before"] == result["after"] == observed


def test_memory_worker_omits_smaps_unless_requested(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)
    requested = []

    def read(path, limit):
        requested.append((path.name, limit))
        return "Name:\tjava\n"

    monkeypatch.setattr(module, "read_proc", read)
    result = module.memory({"target": asdict(TARGET)})
    assert requested == [("status", 65536), ("smaps_rollup", 262144)]
    assert "smaps" not in result
    assert result["errors"] == {}


def test_memory_worker_reads_smaps_when_requested(monkeypatch):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)
    requested = []

    def read(path, limit):
        requested.append((path.name, limit))
        return "7f00-7f01 rw-p 0 00:00 0\nSize: 4 kB\n"

    monkeypatch.setattr(module, "read_proc", read)
    result = module.memory({"target": asdict(TARGET), "include_smaps": True})
    assert requested == [
        ("status", 65536),
        ("smaps_rollup", 262144),
        ("smaps", 8388608),
    ]
    assert result["smaps"] == "7f00-7f01 rw-p 0 00:00 0\nSize: 4 kB\n"


def test_memory_worker_records_an_over_limit_smaps_without_losing_siblings(
    monkeypatch,
):
    module = worker()
    observed = probe()
    monkeypatch.setattr(module, "identity", lambda cfg, **kwargs: observed)

    def read(path, limit):
        if path.name == "smaps":
            raise ValueError("procfs evidence exceeds read bound")
        return f"{path.name}-body"

    monkeypatch.setattr(module, "read_proc", read)
    result = module.memory({"target": asdict(TARGET), "include_smaps": True})
    assert result["smaps"] is None
    assert "exceeds read bound" in result["errors"]["smaps"]
    assert result["status"] == "status-body"
    assert result["smaps_rollup"] == "smaps_rollup-body"


def test_proc_reader_rejects_truncation_and_preserves_original_text(tmp_path):
    module = worker()
    data = tmp_path / "smaps_rollup"
    data.write_text("Pss:                 137 kB\n")
    assert module.read_proc(data, 100) == "Pss:                 137 kB\n"
    with pytest.raises(ValueError, match="procfs evidence exceeds read bound"):
        module.read_proc(data, 3)


def test_host_uid_namespace_denial_is_not_fabricated_namespace_evidence(
    tmp_path, monkeypatch
):
    from nanolab.tasks.soak import diagnostic_helper as helper

    (tmp_path / "status").write_text(
        "Uid:\t65532\t65532\t65532\t65532\nGid:\t65532\t65532\t65532\t65532\nNSpid:\t1234\t1\n"
    )
    (tmp_path / "stat").write_text("1234 (java) S " + "0 " * 18 + "314\n")
    monkeypatch.setattr(helper, "Path", lambda unused: tmp_path)

    def denied(path):
        raise PermissionError("synthetic host UID1000 denial")

    monkeypatch.setattr(helper.os, "readlink", denied)
    value = helper._proc_identity(1234)
    assert value["uid"] == 65532 and value["start_ticks"] == "314"
    assert value["pid_namespace"] is None and value["mount_namespace"] is None


def test_memory_api_returns_raw_bound_sample_using_only_remote_read(
    tmp_path, monkeypatch
):
    from nanolab.tasks.soak import diagnostic_helper as helper

    cfg = spec(
        tmp_path,
        target=replace(TARGET, runtime="native"),
        memory_only=True,
        allow_target_stop_on_cancel=False,
    )
    process = {"start_ticks": "314", "ns_pid": 1}
    before = probe()
    raw = {
        "schema": "nanolab-soak-memory-helper-v1",
        "target": asdict(cfg.target),
        "before": before,
        "after": before,
        "status": "VmRSS:\t512 kB\n",
        "smaps_rollup": "Pss:\t137 kB\n",
        "errors": {},
    }

    class Commands:
        deadline = None
        cleanup_deadline = None

        def inspect(self, identity, **kwargs):
            if kwargs.get("image"):
                return {"RepoDigests": [DIGEST]}
            if identity == CID:
                return container()
            return {
                "Id": "e" * 64,
                "State": {"Pid": 5678},
                "Config": {
                    "Labels": {
                        "nanolab.run": "run-123",
                        "nanolab.diagnostic.target": CID,
                    }
                },
            }

        def run(self, args, **kwargs):
            assert args[:5] == (
                "exec",
                "e" * 64,
                "/usr/local/bin/python3",
                "/opt/nanolab/diagnostic-worker.py",
                "memory",
            )
            return json.dumps(raw)

    monkeypatch.setattr(
        helper, "_proc_identity", lambda pid: process if pid == 1234 else {"ns_pid": 8}
    )
    owner = helper._OwnedDockerHelper(cfg, Commands(), "e" * 64, process)
    result = owner.read_memory(timeout_s=1)
    assert result["smaps_rollup"] == "Pss:\t137 kB\n"
    assert result["status"] == "VmRSS:\t512 kB\n"
    assert result["errors"] == {}
