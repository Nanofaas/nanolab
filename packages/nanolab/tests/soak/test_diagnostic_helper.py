"""Synthetic helper boundaries; no Docker daemon or runtime is contacted."""

import base64
import importlib.util
import json
import tempfile
import time
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


@pytest.fixture
def memory_worker(monkeypatch, tmp_path):
    module = worker()
    checks, order = [], []

    def identity(cfg, **kwargs):
        checks.append(kwargs.get("require_shared_tmp"))
        return probe()

    def read(path, limit):
        order.append(path.name)
        return path.name + "-body"

    monkeypatch.setattr(module, "identity", identity)
    monkeypatch.setattr(module, "read_proc", read)
    monkeypatch.setattr(
        module,
        "TemporaryDirectory",
        lambda **kwargs: tempfile.TemporaryDirectory(dir=tmp_path),
    )
    return module, checks, order


def test_legacy_memory_response_has_no_new_metadata(memory_worker, monkeypatch):
    module, checks, order = memory_worker
    monkeypatch.setattr(
        module, "jcmd", lambda *a, **k: pytest.fail("unexpected attach")
    )
    for options in ({}, {"include_smaps": False, "include_heap_info": False}):
        result = module.memory({"target": asdict(TARGET), **options})
        assert set(result) == {
            "schema",
            "target",
            "source",
            "started_s",
            "errors",
            "before",
            "status",
            "smaps_rollup",
            "after",
            "ended_s",
        }
    assert order == ["status", "smaps_rollup"] * 2
    assert checks == [False] * 4


@pytest.mark.parametrize(
    ("failure", "state"), [(False, "completed"), (True, "completed")]
)
def test_heap_info_follows_procfs_and_records_acknowledged_completion(
    memory_worker,
    monkeypatch,
    failure,
    state,
):
    module, checks, order = memory_worker

    def jcmd(args, deadline, scratch, *, require_completion=False):
        assert args == ("GC.heap_info",)
        assert deadline > time.monotonic()
        assert require_completion
        order.append("heap_info")
        if failure:
            raise module.CommandCompletedError("acknowledged command error")
        return "garbage-first heap total 1024K, used 512K\n"

    monkeypatch.setattr(module, "jcmd", jcmd)
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "include_heap_info": True,
            "memory_deadline_s": time.monotonic() + 5,
        }
    )
    assert order == ["status", "smaps_rollup", "smaps", "heap_info"]
    assert checks == [True, True]
    assert result["completion"]["heap_info"] == state
    assert (result["heap_info"] is None) == failure
    assert set(result["intervals"]) == set(order)
    for interval in result["intervals"].values():
        assert interval["ended_s"] >= interval["started_s"]


def test_worker_does_not_disguise_unresolved_completion(memory_worker, monkeypatch):
    module, _, _ = memory_worker

    def unresolved(*args, **kwargs):
        raise module.CommandCompletionUnresolved("jcmd deadline exceeded")

    monkeypatch.setattr(module, "jcmd", unresolved)
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_heap_info": True,
            "memory_deadline_s": time.monotonic() + 5,
        }
    )
    assert result["completion"]["heap_info"] == "unresolved"
    assert result["heap_info"] is None
    assert result["status"] == "status-body"


def test_expired_budget_never_launches_jcmd(memory_worker, monkeypatch):
    module, _, _ = memory_worker
    monkeypatch.setattr(module, "jcmd", lambda *a, **k: pytest.fail("expired attach"))
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_heap_info": True,
            "memory_deadline_s": time.monotonic() - 1,
        }
    )
    assert result["completion"]["heap_info"] == "not_started"


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("timed_out", True),
        ("forced_stop", True),
        ("cancelled", True),
        ("quota_exceeded", True),
        ("errors", ("runner error",)),
        ("reaped", False),
        ("ended_s", None),
        # A negative returncode is signal death, not an acknowledged error exit.
        ("returncode", -9),
    ],
)
def test_memory_command_requires_acknowledged_completion(
    monkeypatch, tmp_path, flag, value
):
    module = worker()
    import sys
    from types import SimpleNamespace

    from nanolab.tasks.soak import processes

    monkeypatch.setitem(sys.modules, "processes", processes)

    result = {
        "returncode": 0,
        "reaped": True,
        "ended_s": 1.0,
        "forced_stop": False,
        "errors": (),
        "cancelled": False,
        "timed_out": False,
        "quota_exceeded": False,
    }
    result[flag] = value

    class Runner:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            return SimpleNamespace(**result)

    monkeypatch.setattr(processes, "OwnedCommandRunner", Runner)
    with pytest.raises(module.CommandCompletionUnresolved):
        module.command(
            ("unused",), time.monotonic() + 5, tmp_path, require_completion=True
        )


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


def memory_owner(monkeypatch, tmp_path, *, smaps=None, completion="completed"):
    import nanolab.tasks.soak.diagnostic_helper as helper

    cfg = spec(tmp_path)
    process = {"start_ticks": "314", "ns_pid": 1}
    calls, cleanup = [], []

    class Commands:
        deadline = None
        cleanup_deadline = None

        def run(self, args, **kwargs):
            calls.append((tuple(args), kwargs))
            request = json.loads(base64.b64decode(args[-1]))
            payload = {
                "schema": "nanolab-soak-memory-helper-v1",
                "target": asdict(TARGET),
                "before": probe(),
                "after": probe(),
                "status": "VmRSS:\t512 kB\n",
                "smaps_rollup": "Pss:\t137 kB\n",
                "errors": {},
            }
            if request.get("include_smaps"):
                payload["smaps"] = smaps
            if request.get("include_heap_info"):
                payload["heap_info"] = None
                payload["completion"] = {"heap_info": completion}
                payload["errors"]["heap_info"] = "synthetic command error"
            response = json.dumps(payload)
            if len(response.encode()) > kwargs.get("limit", 1048576):
                raise RuntimeError("transport quota exceeded")
            consume = kwargs.get("consume")
            return consume(response) if consume is not None else response

    owner = helper._OwnedDockerHelper(cfg, Commands(), "e" * 64, process)
    monkeypatch.setattr(owner, "_check_target", lambda: None)
    monkeypatch.setattr(owner, "_helper_owned", lambda **kw: {"State": {"Pid": 5678}})
    monkeypatch.setattr(helper, "_proc_identity", lambda pid: {"ns_pid": 8})

    def close():
        cleanup.append("close")
        owner._closed = True

    def cancel_remote():
        cleanup.append("cancel-target")
        close()

    monkeypatch.setattr(owner, "close", close)
    monkeypatch.setattr(owner, "_cancel_remote", cancel_remote)
    return owner, calls, cleanup


def test_read_memory_preserves_legacy_wire_behavior(monkeypatch, tmp_path):
    owner, calls, cleanup = memory_owner(monkeypatch, tmp_path)
    owner.read_memory(timeout_s=1)
    args, kwargs = calls[-1]
    cfg = json.loads(base64.b64decode(args[-1]))
    assert set(cfg) == {"target", "target_start_ticks", "helper_pid"}
    assert kwargs == {}
    assert cleanup == []


@pytest.mark.parametrize("raw", ["x" * 2097152, "\x01" * 8388608])
def test_read_memory_transports_large_escaped_responses(monkeypatch, tmp_path, raw):
    owner, calls, cleanup = memory_owner(monkeypatch, tmp_path, smaps=raw)
    result = owner.read_memory(timeout_s=120, include_smaps=True)
    args, kwargs = calls[-1]
    cfg = json.loads(base64.b64decode(args[-1]))
    assert result["smaps"] == raw
    assert kwargs["limit"] == 67108864
    assert 20 < kwargs["timeout_s"] <= 120
    assert cfg["include_smaps"] is True
    assert "include_heap_info" not in cfg
    assert cleanup == []


def test_worker_deadline_uses_remaining_host_budget(monkeypatch, tmp_path):
    import nanolab.tasks.soak.diagnostic_helper as helper

    owner, calls, _ = memory_owner(monkeypatch, tmp_path)
    now = [100.0]
    monkeypatch.setattr(helper.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(owner, "_check_target", lambda: now.__setitem__(0, 104.0))
    owner.read_memory(timeout_s=5, include_heap_info=True)
    args, kwargs = calls[-1]
    cfg = json.loads(base64.b64decode(args[-1]))
    assert kwargs["timeout_s"] == 1.0
    assert 104.0 < cfg["memory_deadline_s"] < 105.0


def test_unresolved_heap_command_cancels_owned_target(monkeypatch, tmp_path):
    import nanolab.tasks.soak.diagnostic_helper as helper

    owner, _, cleanup = memory_owner(monkeypatch, tmp_path, completion="unresolved")
    with pytest.raises(helper.MemoryCommandUnresolved):
        owner.read_memory(include_heap_info=True)
    assert cleanup == ["cancel-target", "close"]


def test_not_started_heap_command_leaves_the_owned_target_alone(monkeypatch, tmp_path):
    """Only a possibly-running JVM command justifies killing the measured target."""
    owner, _, cleanup = memory_owner(monkeypatch, tmp_path, completion="not_started")
    result = owner.read_memory(include_heap_info=True)
    assert result["completion"]["heap_info"] == "not_started"
    assert cleanup == []
    assert not owner._closed
    owner.read_memory()  # another operation remains possible


def test_completed_source_error_leaves_helper_usable(monkeypatch, tmp_path):
    owner, _, cleanup = memory_owner(monkeypatch, tmp_path)
    result = owner.read_memory(include_heap_info=True)
    assert result["errors"]["heap_info"]
    assert not owner._closed
    assert cleanup == []
    owner.read_memory()  # another operation remains possible


@pytest.mark.parametrize("error", [RuntimeError("lost reply"), KeyboardInterrupt()])
def test_lost_reply_or_cancel_preserves_exception_and_stops_target(
    monkeypatch,
    tmp_path,
    error,
):
    owner, _, cleanup = memory_owner(monkeypatch, tmp_path)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(owner.commands, "run", fail)
    with pytest.raises(type(error)) as raised:
        owner.read_memory(include_heap_info=True)
    assert raised.value is error
    assert cleanup == ["cancel-target", "close"]


def test_local_memory_request_keeps_the_remaining_absolute_deadline(
    monkeypatch, tmp_path
):
    import nanolab.tasks.soak.diagnostic_helper as helper

    owner, calls, _ = memory_owner(monkeypatch, tmp_path)
    now = [100.0]
    monkeypatch.setattr(helper.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(owner, "_check_target", lambda: now.__setitem__(0, 104.0))
    owner.read_memory(timeout_s=5, include_smaps=True)
    argv, options = calls[-1]
    cfg = json.loads(base64.b64decode(argv[-1]))
    assert "memory_budget_s" not in cfg
    assert 104.0 < cfg["memory_deadline_s"] < 105.0
    assert options["timeout_s"] == 1.0


def test_late_worker_does_not_restart_the_collection_budget(monkeypatch):
    module = worker()
    monkeypatch.setattr(module.time, "monotonic", lambda: 112.0)
    monkeypatch.setattr(module, "identity", lambda *a, **k: probe())
    monkeypatch.setattr(module, "read_proc", lambda *a: pytest.fail("expired read"))
    monkeypatch.setattr(module, "jcmd", lambda *a, **k: pytest.fail("expired attach"))
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "include_heap_info": True,
            "memory_deadline_s": 109.0,
        }
    )
    assert all(value == "not_started" for value in result["completion"].values())
    assert result["heap_info"] is None


def test_procfs_and_heap_info_share_one_deadline(monkeypatch):
    module = worker()
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module, "identity", lambda *a, **k: probe())

    def read(path, limit):
        now[0] += 3.0
        return "body"

    monkeypatch.setattr(module, "read_proc", read)
    monkeypatch.setattr(module, "jcmd", lambda *a, **k: pytest.fail("late attach"))
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_heap_info": True,
            "memory_deadline_s": 105.0,
        }
    )
    assert result["completion"]["heap_info"] == "not_started"


def logged_memory_owner(monkeypatch, tmp_path, *, raw=None, returncode=0):
    from threading import Event

    from nanolab.tasks.soak import diagnostic_helper as helper
    from nanolab.tasks.soak.processes import OwnedCommandResult

    owner, _, cleanup = memory_owner(monkeypatch, tmp_path)
    payload = (
        raw
        if raw is not None
        else json.dumps(
            {
                "schema": "nanolab-soak-memory-helper-v1",
                "target": asdict(TARGET),
                "before": probe(),
                "after": probe(),
                "errors": {},
                "status": "VmRSS: 4 kB\n",
                "smaps_rollup": "Pss: 4 kB\n",
                "smaps": "body",
                "heap_info": None,
                "completion": {"heap_info": "completed"},
            }
        )
    )

    class Runner:
        def __init__(self, *args, **kwargs):
            self.log = kwargs["log_path"]

        def run(self):
            self.log.write_text(payload)
            return OwnedCommandResult(returncode, False, True, ended_s=1.0)

    monkeypatch.setattr(helper, "OwnedCommandRunner", Runner)
    # The genuine transport replaces memory_owner's fake on purpose.
    owner.commands = helper._DockerCommands(  # pyright: ignore[reportAttributeAccessIssue]
        owner.spec, tmp_path, Event()
    )
    return owner, cleanup


def test_accepted_optional_response_removes_duplicate_log(monkeypatch, tmp_path):
    owner, _ = logged_memory_owner(monkeypatch, tmp_path)
    assert owner.read_memory(include_smaps=True)["smaps"] == "body"
    assert not list(tmp_path.glob("docker-*.log"))


@pytest.mark.parametrize(
    ("raw", "returncode"), [("not JSON", 0), ("command failed", 1)]
)
def test_rejected_optional_response_keeps_log(monkeypatch, tmp_path, raw, returncode):
    owner, _ = logged_memory_owner(
        monkeypatch, tmp_path, raw=raw, returncode=returncode
    )
    with pytest.raises((ValueError, RuntimeError)):
        owner.read_memory(include_smaps=True)
    assert list(tmp_path.glob("docker-*.log"))


def test_unresolved_response_keeps_log_even_with_zero_exit(monkeypatch, tmp_path):
    payload = {
        "schema": "nanolab-soak-memory-helper-v1",
        "target": asdict(TARGET),
        "before": probe(),
        "after": probe(),
        "completion": {"heap_info": "unresolved"},
    }
    owner, cleanup = logged_memory_owner(monkeypatch, tmp_path, raw=json.dumps(payload))
    with pytest.raises(RuntimeError, match="completion unresolved"):
        owner.read_memory(include_heap_info=True)
    assert list(tmp_path.glob("docker-*.log"))
    assert cleanup == ["cancel-target", "close"]


def test_legacy_memory_log_is_retained(monkeypatch, tmp_path):
    owner, _ = logged_memory_owner(monkeypatch, tmp_path)
    owner.read_memory()
    assert list(tmp_path.glob("docker-*.log"))


def test_unresolved_memory_cleanup_receives_ten_seconds(monkeypatch, tmp_path):
    from nanolab.tasks.soak import diagnostic_helper as helper

    owner, _, _ = memory_owner(monkeypatch, tmp_path, completion="unresolved")
    monkeypatch.setattr(helper.time, "monotonic", lambda: 100.0)
    budgets = []

    def record_budget():
        deadline = owner.commands.cleanup_deadline
        assert deadline is not None
        budgets.append(deadline - 100.0)

    monkeypatch.setattr(owner, "_cancel_remote", record_budget)
    with pytest.raises(helper.MemoryCommandUnresolved):
        owner.read_memory(include_heap_info=True)
    assert budgets == [10.0]


def test_cleanup_failure_keeps_the_original_interrupt(monkeypatch, tmp_path):
    owner, _, _ = memory_owner(monkeypatch, tmp_path)
    interrupted = KeyboardInterrupt("user cancelled")

    def fail_read(*args, **kwargs):
        raise interrupted

    def fail_cleanup():
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(owner.commands, "run", fail_read)
    monkeypatch.setattr(owner, "_cancel_remote", fail_cleanup)
    with pytest.raises(KeyboardInterrupt) as raised:
        owner.read_memory(include_heap_info=True)
    assert raised.value is interrupted
    assert any("cleanup unconfirmed" in note for note in interrupted.__notes__)


def test_failed_procfs_read_has_a_distinct_state(monkeypatch):
    module = worker()
    monkeypatch.setattr(module, "identity", lambda *a, **k: probe())

    def read(path, limit):
        if path.name == "smaps":
            raise ValueError("procfs evidence exceeds read bound")
        return "body"

    monkeypatch.setattr(module, "read_proc", read)
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "memory_deadline_s": time.monotonic() + 30,
        }
    )
    assert result["completion"]["smaps"] == "failed"
    assert result["completion"]["status"] == "completed"
    assert "exceeds read bound" in result["errors"]["smaps"]


@pytest.mark.parametrize(
    ("code", "exception_name"),
    [
        (None, "CommandCompletionUnresolved"),
        (-9, "CommandCompletionUnresolved"),
        (1, "CommandCompletedError"),
        (0, None),
    ],
)
def test_command_exit_code_preserves_completion_semantics(
    monkeypatch, tmp_path, code, exception_name
):
    import sys

    from nanolab.tasks.soak import processes

    module = worker()
    result = processes.OwnedCommandResult(
        returncode=code,
        forced_stop=False,
        reaped=True,
        ended_s=1.0,
    )

    class Runner:
        def __init__(self, *args, **kwargs):
            kwargs["log_path"].write_text("acknowledged output")

        def run(self):
            return result

    monkeypatch.setitem(sys.modules, "processes", processes)
    monkeypatch.setattr(processes, "OwnedCommandRunner", Runner)
    if exception_name is None:
        assert (
            module.command(
                ("unused",), time.monotonic() + 30, tmp_path, require_completion=True
            )
            == "acknowledged output"
        )
    else:
        with pytest.raises(getattr(module, exception_name)):
            module.command(
                ("unused",), time.monotonic() + 30, tmp_path, require_completion=True
            )


def test_runner_failure_before_launch_is_not_started(memory_worker, monkeypatch):
    """A failure without a launched child leaves the target untouched.

    Reported as unresolved, the host would SIGKILL the measured container for a
    command that never reached the JVM.
    """
    import sys

    from nanolab.tasks.soak import processes

    module, _, _ = memory_worker

    class Runner:
        launched = False

        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            raise OSError("cannot create the owned log directory")

    monkeypatch.setitem(sys.modules, "processes", processes)
    monkeypatch.setattr(processes, "OwnedCommandRunner", Runner)
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "include_heap_info": True,
            "memory_deadline_s": time.monotonic() + 5,
        }
    )
    assert result["completion"]["heap_info"] == "not_started"
    assert result["heap_info"] is None
    assert "OSError" in result["errors"]["heap_info"]


def test_runner_failure_after_launch_stays_unresolved(memory_worker, monkeypatch):
    """A launched child means the in-JVM command may still be running."""
    import sys

    from nanolab.tasks.soak import processes

    module, _, _ = memory_worker

    class Runner:
        launched = True

        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            raise OSError("supervisor receipt was lost after launch")

    monkeypatch.setitem(sys.modules, "processes", processes)
    monkeypatch.setattr(processes, "OwnedCommandRunner", Runner)
    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "include_heap_info": True,
            "memory_deadline_s": time.monotonic() + 5,
        }
    )
    assert result["completion"]["heap_info"] == "unresolved"
    assert result["heap_info"] is None


def test_expired_command_deadline_never_starts_the_target(tmp_path, monkeypatch):
    import sys

    from nanolab.tasks.soak import processes

    module = worker()
    monkeypatch.setitem(sys.modules, "processes", processes)
    with pytest.raises(module.CommandNotStartedError, match="deadline exhausted"):
        module.command(
            ("unused",), time.monotonic() - 1, tmp_path, require_completion=True
        )


def test_acknowledged_error_carries_a_bounded_log_excerpt(
    memory_worker, monkeypatch, tmp_path
):
    """A failed jcmd must say why somewhere in the published evidence."""
    import sys

    from nanolab.tasks.soak import processes

    module, _, _ = memory_worker
    excerpt = "Attach refused: the target JVM does not respond"
    outcome = processes.OwnedCommandResult(
        returncode=1,
        forced_stop=False,
        reaped=True,
        ended_s=1.0,
    )

    class Runner:
        launched = True

        def __init__(self, *args, **kwargs):
            kwargs["log_path"].write_text(excerpt + "\n" + "x" * 4096)

        def run(self):
            return outcome

    monkeypatch.setitem(sys.modules, "processes", processes)
    monkeypatch.setattr(processes, "OwnedCommandRunner", Runner)
    with pytest.raises(module.CommandCompletedError) as caught:
        module.command(
            ("unused",), time.monotonic() + 30, tmp_path, require_completion=True
        )
    message = str(caught.value)
    assert excerpt in message
    assert len(message) <= 1024 + len("remote command exited with an error: ")

    result = module.memory(
        {
            "target": asdict(TARGET),
            "include_smaps": True,
            "include_heap_info": True,
            "memory_deadline_s": time.monotonic() + 5,
        }
    )
    assert result["completion"]["heap_info"] == "completed"
    assert result["heap_info"] is None
    assert excerpt in result["errors"]["heap_info"]
    assert len(result["errors"]["heap_info"]) <= 1024
