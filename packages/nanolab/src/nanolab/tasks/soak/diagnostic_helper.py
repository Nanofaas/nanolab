"""Provision real, owned local-Docker diagnostic helpers.

No Docker work occurs at import or construction. The operator calls prepare and
owns normal teardown. Receipts record observations, not independent attestation.
Only container-init targets are supported: Target.process_id is the HOST PID.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

from nanolab.tasks.soak.diagnostic_exec import (
    HelperProvisioning,
    ProvisionedDiagnosticExecutor,
)
from nanolab.tasks.soak.diagnostics import (
    DiagnosticBudget,
    JvmDiagnosticAdapter,
    NodeDiagnosticAdapter,
    supported_operations,
)
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.processes import OwnedCommandRunner

SCHEMA = "nanolab-soak-diagnostic-helper-v1"
WORKER = "/opt/nanolab/diagnostic-worker.py"
PYTHON = "/usr/local/bin/python3"
NODE_SOCKET = "/tmp/nanolab-diagnostic.sock"  # nosec B108 - isolated container path
GC_SOURCES = {"jvm": "jdk.GarbageCollection", "node": "node:perf_hooks:major-gc"}
_DIGEST = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_OPT_IN_RESPONSE_BYTES = 67108864


class MemoryCommandUnresolved(RuntimeError):  # noqa: N818
    """No acknowledgement permits a later diagnostic on this target."""


@dataclass(frozen=True)
class DockerHelperSpec:
    """Operator authorization and immutable target/helper binding.

    helper_image must already exist locally by RepoDigest. An interrupted
    in-process diagnostic requires stopping the target; permission is mandatory.
    Node must preload node-diagnostic-control.cjs before application startup.
    """

    target: Target
    helper_image: str
    owner_label: str
    owner_value: str
    uid: int
    gid: int
    output_root: Path
    quota_bytes: int
    helper_memory_bytes: int
    allow_target_stop_on_cancel: bool
    docker: str = "/usr/bin/docker"
    docker_host: str = "unix:///var/run/docker.sock"
    node_socket: str = NODE_SOCKET
    architecture: str = "arm64"
    memory_only: bool = False
    target_tmp_volume: str | None = None

    def __post_init__(self):
        """Reject a spec whose owned mounts are not fully identified."""
        if not self.memory_only and (
            not isinstance(self.target_tmp_volume, str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.target_tmp_volume
            )
        ):
            raise ValueError(
                "diagnostics require an explicit owned target_tmp_volume name"
            )
        if (
            self.target.runtime
            not in (set(GC_SOURCES) | ({"native"} if self.memory_only else set()))
            or not re.fullmatch(r"[0-9a-f]{64}", self.target.container_id)
            or not _DIGEST.fullmatch(self.helper_image)
            or not _DIGEST.fullmatch(self.target.image_digest)
            or type(self.target.process_id) is not int
            or self.target.process_id <= 0
            or not self.target.process_started_at
        ):
            raise ValueError(
                "exact owned container-init identity and pinned images required"
            )
        if (
            not self.owner_label
            or not self.owner_value
            or type(self.memory_only) is not bool
            or (not self.memory_only and self.allow_target_stop_on_cancel is not True)
        ):
            raise ValueError(
                "owner label and explicit target-stop cancellation permission required"
            )
        if any(type(n) is not int or n < 0 for n in (self.uid, self.gid)):
            raise ValueError("matching numeric UID/GID required")
        if (
            type(self.quota_bytes) is not int
            or self.quota_bytes < 4096
            or self.quota_bytes % 4096
            or type(self.helper_memory_bytes) is not int
            or self.helper_memory_bytes <= self.quota_bytes
        ):
            raise ValueError(
                "page-aligned quota and helper memory above quota required"
            )
        if (
            not self.output_root.is_absolute()
            or self.output_root.is_symlink()
            or not self.output_root.is_dir()
            or not Path(self.docker).is_absolute()
            or not self.docker_host.startswith("unix:///")
            or self.architecture not in {"arm64", "amd64"}
            or not re.fullmatch(r"/tmp/[A-Za-z0-9_.-]+", self.node_socket)  # nosec B108 - isolated container path
        ):
            raise ValueError(
                "local Docker, owned output directory and private /tmp socket required"
            )


def helper_create_argv(spec: DockerHelperSpec, name: str) -> tuple[str, ...]:
    """Return a restricted create command; never pull or join host namespaces."""
    mounts = (
        ()
        if spec.memory_only
        else (
            "--mount",
            f"type=volume,source={spec.target_tmp_volume},target=/tmp,volume-nocopy",
            "--mount",
            f"type=bind,source={spec.output_root},target={spec.output_root},readonly",
        )
    )
    return (
        "create",
        "--pull=never",
        "--name",
        name,
        "--label",
        f"{spec.owner_label}={spec.owner_value}",
        "--label",
        "nanolab.diagnostic.target=" + spec.target.container_id,
        "--pid",
        "container:" + spec.target.container_id,
        "--network",
        "none",
        "--ipc",
        "private",
        "--cgroupns",
        "private",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--read-only",
        "--user",
        f"{spec.uid}:{spec.gid}",
        "--memory",
        str(spec.helper_memory_bytes),
        "--memory-swap",
        str(spec.helper_memory_bytes),
        "--pids-limit",
        "64",
        "--tmpfs",
        f"/out:rw,noexec,nosuid,nodev,size={spec.quota_bytes},mode=0700,uid={spec.uid},gid={spec.gid}",
        "--tmpfs",
        f"/work:rw,noexec,nosuid,nodev,size=16777216,mode=0700,uid={spec.uid},gid={spec.gid}",
        *mounts,
        "--entrypoint",
        PYTHON,
        spec.helper_image,
        WORKER,
        "hold",
    )


def validate_container(spec: DockerHelperSpec, data: dict, image: dict) -> None:
    """Reject ownership, host-PID, restart, image or namespace drift."""
    state, config, host = data["State"], data["Config"], data["HostConfig"]
    if (
        data["Id"] != spec.target.container_id
        or config.get("Labels", {}).get(spec.owner_label) != spec.owner_value
        or state.get("Running") is not True
        or state.get("Pid") != spec.target.process_id
        or state.get("StartedAt") != spec.target.process_started_at
        or spec.target.image_digest not in (image.get("RepoDigests") or [])
        or host.get("Privileged") is not False
        or any(
            host.get(key) == "host"
            for key in ("PidMode", "NetworkMode", "IpcMode", "UTSMode", "CgroupnsMode")
        )
        or host.get("RestartPolicy", {}).get("Name", "no") not in {"", "no"}
        or any(
            cap.upper().removeprefix("CAP_") in {"SYS_ADMIN", "ALL"}
            for cap in (host.get("CapAdd") or [])
        )
    ):
        raise ValueError("owned target identity or isolation changed")


def validate_target_tmp_volume(
    spec: DockerHelperSpec, volume: dict, target: dict
) -> int:
    """Require an existing owned local tmpfs and the target's actual volume mount.

    Options are intentionally strict: decimal byte capacity, 4 KiB through 1 GiB,
    matching UID/GID, sticky mode and all three restrictive mount flags. No bind,
    percentage/default capacity, plugin driver or duplicate option is accepted.
    """
    labels = volume.get("Labels") or {}
    options = volume.get("Options") or {}
    if (
        volume.get("Name") != spec.target_tmp_volume
        or volume.get("Driver") != "local"
        or volume.get("Scope") != "local"
        or not isinstance(volume.get("CreatedAt"), str)
        or not volume["CreatedAt"]
        or labels.get(spec.owner_label) != spec.owner_value
        or labels.get("nanolab.diagnostic.tmp") != "true"
        or set(options) != {"type", "device", "o"}
        or options.get("type") != "tmpfs"
        or options.get("device") != "tmpfs"
        or not isinstance(options.get("o"), str)
        or not isinstance(volume.get("Mountpoint"), str)
        or not volume["Mountpoint"].startswith("/")
    ):
        raise ValueError("existing exact owned local tmpfs volume required")
    parsed = {}
    for option in options["o"].split(","):
        key, separator, value = option.partition("=")
        if key in parsed:
            raise ValueError("duplicate tmpfs mount option")
        parsed[key] = value if separator else None
    if (
        set(parsed) != {"size", "uid", "gid", "mode", "noexec", "nosuid", "nodev"}
        or parsed.get("uid") != str(spec.uid)
        or parsed.get("gid") != str(spec.gid)
        or parsed.get("mode") != "1777"
        or any(parsed.get(flag) is not None for flag in ("noexec", "nosuid", "nodev"))
        or not re.fullmatch(r"[0-9]{1,10}", parsed.get("size") or "")
    ):
        raise ValueError("explicit finite restrictive tmpfs options required")
    capacity = int(parsed["size"])
    if not 4096 <= capacity <= 1024 * 1024 * 1024 or capacity % 4096:
        raise ValueError(
            "target tmpfs capacity must be page-aligned and within 4 KiB..1 GiB"
        )
    mounts = [
        item
        for item in target.get("Mounts", [])
        if item.get("Destination") == "/tmp"  # nosec B108 - isolated container path
    ]
    if len(mounts) != 1 or any(
        mounts[0].get(key) != expected
        for key, expected in {
            "Type": "volume",
            "Name": spec.target_tmp_volume,
            "Driver": "local",
            "Source": volume["Mountpoint"],
            "RW": True,
        }.items()
    ):
        raise ValueError("actual /tmp mount must be the same writable owned volume")
    return capacity


def _inspect_target_tmp_volume(commands, spec, target):
    observed = json.loads(commands.run(("volume", "inspect", spec.target_tmp_volume)))
    if not isinstance(observed, list) or len(observed) != 1:
        raise ValueError("exact existing volume inspection required")
    capacity = validate_target_tmp_volume(spec, observed[0], target)
    if spec.target.runtime == "jvm" and capacity > spec.quota_bytes:
        raise ValueError(
            "shared JVM tmpfs quota exceeds the diagnostic output reservation"
        )
    return observed[0]


def validate_probe(spec: DockerHelperSpec, probe: dict) -> int:
    """Derive capabilities only from the pinned worker's actual observations."""
    if (
        probe.get("runtime") != spec.target.runtime
        or probe.get("uid") != spec.uid
        or probe.get("target_uid") != spec.uid
        or probe.get("gid") != spec.gid
        or probe.get("target_gid") != spec.gid
        or probe.get("target_pid") != 1
        or not re.fullmatch(r"[0-9]+", str(probe.get("target_start_ticks", "")))
        or not probe.get("pid_namespace")
        or probe["pid_namespace"] != probe.get("target_pid_namespace")
        or not probe.get("mount_namespace")
        or not probe.get("target_mount_namespace")
        or type(probe.get("helper_pid")) is not int
        or probe["helper_pid"] <= 1
        or not probe.get("attach_output")
    ):
        raise ValueError("unverified runtime attachment or namespace identity")
    if spec.target.runtime == "jvm":
        if not str(probe.get("version", "")).startswith("25.") or not str(
            probe.get("helper_version", "")
        ).startswith("25."):
            raise ValueError("compatible JVM25 target and helper required")
    elif (
        probe.get("private_control") is not True
        or probe.get("inspector_url") is not None
        or not re.fullmatch(r"\d+\.\d+\.\d+", str(probe.get("version", "")))
    ):
        raise ValueError("Node control must be an in-process private session")
    quota = probe.get("quota", {})
    capacity = quota.get("capacity_bytes")
    if (
        quota.get("type") != "tmpfs"
        or type(capacity) is not int
        or not 0 < capacity <= spec.quota_bytes
        or type(quota.get("target_write_bytes")) is not int
        or quota["target_write_bytes"] <= 0
    ):
        raise ValueError("kernel quota and actual target-side write required")
    return capacity


def _encoded(value: dict) -> str:
    return base64.b64encode(json.dumps(value, allow_nan=False).encode()).decode()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


class _DockerCommands:
    def __init__(self, spec: DockerHelperSpec, root: Path, cancelled: Event):
        self.spec, self.root, self.cancelled = spec, root, cancelled
        self.deadline: float | None = None
        self.cleanup_deadline: float | None = None

    def run(
        self, args, timeout_s=20.0, *, cleanup=False, limit=1024 * 1024, consume=None
    ):
        deadline = self.cleanup_deadline if cleanup else self.deadline
        if deadline is not None:
            timeout_s = min(timeout_s, deadline - time.monotonic())
        if timeout_s <= 0:
            raise TimeoutError("owned Docker operation deadline exhausted")
        log = self.root / ("docker-" + uuid4().hex + ".log")
        result = OwnedCommandRunner(
            (self.spec.docker, "--host", self.spec.docker_host, *args),
            cwd=self.root,
            env={
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("DOCKER_", "PYTHON", "LD_"))
            },
            log_path=log,
            timeout_s=timeout_s,
            cancelled=Event() if cleanup else self.cancelled,
            output_limit_bytes=limit,
            stop_timeout_s=1.0,
        ).run()
        if (
            result.returncode != 0
            or not result.reaped
            or result.ended_s is None
            or result.errors
            or result.cancelled
            or result.timed_out
            or result.forced_stop
            or result.quota_exceeded
        ):
            raise RuntimeError(f"owned Docker command failed; evidence: {log}")
        text = log.read_text()
        if consume is None:
            return text
        accepted = consume(text)
        log.unlink(missing_ok=True)
        return accepted

    def inspect(self, identity, *, image=False, cleanup=False):
        value = json.loads(
            self.run(
                ("image" if image else "container", "inspect", identity),
                cleanup=cleanup,
            )
        )
        if not isinstance(value, list) or len(value) != 1:
            raise ValueError("exact Docker inspection required")
        return value[0]


def _proc_identity(pid: int) -> dict:
    root = Path(f"/proc/{pid}")
    status = dict(
        line.split(":", 1)
        for line in (root / "status").read_text().splitlines()
        if ":" in line
    )

    def namespace(name):
        try:
            return str((root / "ns" / name).readlink())
        except PermissionError:
            # A UID1000 host cannot necessarily inspect UID0/65532 namespaces.
            # The same-UID worker must supply the namespace evidence instead.
            return None

    return {
        "uid": int(status["Uid"].split()[1]),
        "gid": int(status["Gid"].split()[1]),
        "ns_pid": int(status["NSpid"].split()[-1]),
        "start_ticks": (root / "stat").read_text().rsplit(")", 1)[1].split()[19],
        "pid_namespace": namespace("pid"),
        "mount_namespace": namespace("mnt"),
    }


class _OwnedDockerHelper:
    """Shared ownership, identity and remote lifetime for diagnostic/memory use."""

    def __init__(self, spec, commands, helper_id, process_identity):
        self.spec, self.commands, self.helper_id = spec, commands, helper_id
        self.process_identity = process_identity
        self._serial = Lock()
        self._closed = False

    def _check_target(self):
        data = self.commands.inspect(self.spec.target.container_id)
        validate_container(
            self.spec, data, self.commands.inspect(data["Image"], image=True)
        )
        if _proc_identity(self.spec.target.process_id) != self.process_identity:
            raise ValueError("target process incarnation or namespaces changed")

    def _helper_owned(self, *, cleanup=True):
        data = self.commands.inspect(self.helper_id, cleanup=cleanup)
        labels = data["Config"].get("Labels") or {}
        if (
            data["Id"] != self.helper_id
            or labels.get(self.spec.owner_label) != self.spec.owner_value
            or labels.get("nanolab.diagnostic.target") != self.spec.target.container_id
        ):
            raise ValueError("helper ownership changed; cleanup refused")
        return data

    def cancel_remote(self, timeout_s=10.0):
        """Stop the exact owned target before removing a possible remote writer."""
        if self._closed:
            return
        if self.spec.memory_only:
            self.close()
            return
        previous = self.commands.cleanup_deadline
        self.commands.cleanup_deadline = time.monotonic() + timeout_s
        try:
            self._cancel_remote()
        finally:
            self.commands.cleanup_deadline = previous

    def _cancel_remote(self):
        target = self.commands.inspect(self.spec.target.container_id, cleanup=True)
        labels = target["Config"].get("Labels") or {}
        if (
            target["Id"] != self.spec.target.container_id
            or labels.get(self.spec.owner_label) != self.spec.owner_value
            or target["State"].get("StartedAt") != self.spec.target.process_started_at
        ):
            raise ValueError("target changed; remote cancellation is unconfirmed")
        if target["State"].get("Running"):
            if (
                _proc_identity(self.spec.target.process_id) != self.process_identity
                or target["State"].get("Pid") != self.spec.target.process_id
            ):
                raise ValueError("target PID changed; remote cancellation refused")
            self.commands.run(
                ("kill", "--signal", "KILL", self.spec.target.container_id),
                cleanup=True,
            )
        stopped = self.commands.inspect(self.spec.target.container_id, cleanup=True)
        if stopped["State"].get("Running") is not False:
            raise RuntimeError("remote diagnostic target stop is unconfirmed")
        self.close()

    def close(self):
        """Remove only this owned helper, retaining host artifacts and receipts."""
        if self._closed:
            return
        self._helper_owned()
        self.commands.run(("rm", "--force", self.helper_id), cleanup=True)
        # Docker rm returns only once the container is removed. Do not infer this
        # from a killed CLI or kill unrelated targets during ordinary teardown.
        self._closed = True

    def read_memory(
        self, timeout_s=5.0, *, include_smaps=False, include_heap_info=False
    ):
        """Return raw target procfs data; unavailable PSS is never zero-filled."""
        if not 0 < timeout_s <= 300:
            raise ValueError("memory collection timeout must be in (0, 300]")
        if include_heap_info and (
            self.spec.memory_only
            or self.spec.target.runtime != "jvm"
            or not self.spec.allow_target_stop_on_cancel
        ):
            raise ValueError("heap-info requires an owned JVM diagnostic helper")
        started = time.monotonic()
        if not self._serial.acquire(timeout=timeout_s):
            raise TimeoutError("helper busy with another operation")
        self.commands.deadline = started + timeout_s
        jvm_may_be_running = False
        try:
            if self._closed:
                raise RuntimeError("memory helper is closed")
            self._check_target()
            remote = self._helper_owned(cleanup=False)
            holder = _proc_identity(remote["State"]["Pid"])
            cfg = {
                "target": asdict(self.spec.target),
                "target_start_ticks": self.process_identity["start_ticks"],
                "helper_pid": holder["ns_pid"],
            }
            options = {}
            if include_smaps or include_heap_info:
                remaining = self.commands.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("memory collection deadline exhausted")
                margin = min(1.0, remaining / 5)
                cfg["memory_deadline_s"] = self.commands.deadline - margin
                if include_smaps:
                    cfg["include_smaps"] = True
                if include_heap_info:
                    cfg["include_heap_info"] = True
                options = {"timeout_s": remaining, "limit": _OPT_IN_RESPONSE_BYTES}
            argv = ("exec", self.helper_id, PYTHON, WORKER, "memory", _encoded(cfg))

            def accept(text):
                nonlocal jvm_may_be_running
                data = json.loads(text)
                self._check_target()
                before, after = data.get("before", {}), data.get("after", {})
                if (
                    data.get("schema") != "nanolab-soak-memory-helper-v1"
                    or data.get("target") != asdict(self.spec.target)
                    or before != after
                    or before.get("target_start_ticks")
                    != self.process_identity["start_ticks"]
                    or before.get("target_pid") != 1
                    or before.get("uid") != self.spec.uid
                    or before.get("target_uid") != self.spec.uid
                    or not before.get("pid_namespace")
                    or before["pid_namespace"] != before.get("target_pid_namespace")
                ):
                    raise ValueError(
                        "memory response process/namespace identity changed"
                    )
                if include_heap_info:
                    state = data.get("completion", {}).get("heap_info")
                    if state not in {"completed", "not_started"}:
                        raise MemoryCommandUnresolved(
                            "in-JVM command completion unresolved"
                        )
                    jvm_may_be_running = False
                return data

            jvm_may_be_running = include_heap_info
            if include_smaps or include_heap_info:
                return self.commands.run(argv, **options, consume=accept)
            return accept(self.commands.run(argv, **options))
        except BaseException as error:
            try:
                self.commands.cleanup_deadline = time.monotonic() + (
                    10.0 if jvm_may_be_running else 5.0
                )
                if jvm_may_be_running:
                    # This validates ownership, stops the exact target, confirms
                    # it stopped, then removes the helper. close() alone is not enough.
                    self._cancel_remote()
                else:
                    self.close()
            except BaseException as cleanup_error:
                if include_smaps or include_heap_info:
                    error.add_note(f"memory cleanup unconfirmed: {cleanup_error}")
                    raise error from cleanup_error
                raise RuntimeError(
                    "memory read failed; remote reader cleanup unconfirmed: "
                    f"{cleanup_error}"
                ) from error
            raise
        finally:
            self.commands.deadline = None
            self.commands.cleanup_deadline = None
            self._serial.release()


class LocalDockerDiagnosticExecutor(_OwnedDockerHelper, ProvisionedDiagnosticExecutor):
    """Use existing framing/supervision and explicitly cancel remote writers."""

    def __init__(self, provisioning, spec, commands, helper_id, process_identity):
        """Bind one provisioned helper to the target it may diagnose."""
        ProvisionedDiagnosticExecutor.__init__(self, provisioning)
        _OwnedDockerHelper.__init__(self, spec, commands, helper_id, process_identity)

    def _exchange(self, message, timeout_s, **kwargs):
        with self._serial:
            if self._closed:
                raise RuntimeError("diagnostic helper is closed")
            cleanup_budget = min(5.0, timeout_s * 0.25)
            deadline = time.monotonic() + timeout_s - cleanup_budget
            self.commands.deadline = deadline
            try:
                self._check_target()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("identity check consumed diagnostic deadline")
                result = super()._exchange(message, remaining, **kwargs)
                if result.get("remote_finished") is not True:
                    raise RuntimeError("remote diagnostic completion is unconfirmed")
                return result
            except BaseException as error:
                try:
                    self.cancel_remote(cleanup_budget)
                except BaseException as cleanup_error:
                    raise RuntimeError(
                        f"diagnostic failed; REMOTE CANCELLATION UNCONFIRMED: "
                        f"{cleanup_error}"
                    ) from error
                raise
            finally:
                self.commands.deadline = None


@dataclass(frozen=True)
class PreparedDockerDiagnostics:
    """Runtime integration handle; capture still uses existing natural gates."""

    executor: LocalDockerDiagnosticExecutor
    receipt: Path
    helper_id: str
    quota_bytes: int

    def adapter(
        self,
        *,
        budget: DiagnosticBudget,
        natural_checkpoint: Path,
        max_capture_bytes: int,
        natural_phase: str = "drain",
    ):
        """Create the existing role adapter with the observed evidence source."""
        if max_capture_bytes < self.quota_bytes:
            raise ValueError("capture budget smaller than provisioned target quota")
        runtime = self.executor.spec.target.runtime
        args = (self.executor.capabilities(), self.executor, budget)
        kwargs = {
            "natural_checkpoint": natural_checkpoint,
            "max_capture_bytes": max_capture_bytes,
            "full_gc_source": GC_SOURCES[runtime],
            "natural_phase": natural_phase,
        }
        if runtime == "jvm":
            return JvmDiagnosticAdapter(*args, command_prefix=("jcmd",), **kwargs)
        return NodeDiagnosticAdapter(*args, **kwargs)

    def close(self):
        """Release the owned helper during main's teardown."""
        self.executor.close()

    def read_memory(
        self, timeout_s=5.0, *, include_smaps=False, include_heap_info=False
    ):
        """Collect raw RSS/PSS evidence through the existing same-UID helper."""
        if not include_smaps and not include_heap_info:
            return self.executor.read_memory(timeout_s)
        return self.executor.read_memory(
            timeout_s,
            include_smaps=include_smaps,
            include_heap_info=include_heap_info,
        )

    def cancel(self, timeout_s=10.0):
        """Explicitly cancel remote writers when main aborts a diagnostic."""
        self.executor.cancel_remote(timeout_s)


@dataclass(frozen=True)
class PreparedDockerMemory:
    """Read-only helper for any supported runtime, including native targets."""

    _owner: _OwnedDockerHelper
    helper_id: str
    initial_sample: dict
    evidence: Path

    def read_memory(
        self, timeout_s=5.0, *, include_smaps=False, include_heap_info=False
    ):
        """Return raw procfs data with identity and explicit availability errors."""
        if not include_smaps and not include_heap_info:
            return self._owner.read_memory(timeout_s)
        return self._owner.read_memory(
            timeout_s,
            include_smaps=include_smaps,
            include_heap_info=include_heap_info,
        )

    def close(self):
        """Cancel/remove only the owned helper; never stop the measured target."""
        self._owner.close()

    def cancel(self, timeout_s=10.0):
        """Cancel a reader by removing the exact owned helper container."""
        self._owner.cancel_remote(timeout_s)


class LocalDockerDiagnosticProvisioner:
    """Prepare only explicitly authorized local containers; never build images."""

    def __init__(self, *, assets_dir: Path, cancelled: Event | None = None):
        """Bind the asset directory this provisioner may copy helpers from."""
        self.assets_dir = assets_dir.resolve()
        self.cancelled = cancelled if cancelled is not None else Event()

    def prepare(self, spec: DockerHelperSpec, *, timeout_s: float = 60.0):
        """Inspect, create, attach/probe, then issue an operator-trusted receipt.

        Failure after attachment begins explicitly cancels the remote target.
        The helper identity is journaled before starting it for main's teardown.
        """
        if spec.memory_only:
            raise ValueError("memory-only specification requires prepare_memory")
        if not 0 < timeout_s <= 300:
            raise ValueError("provision timeout must be in (0, 300]")
        deadline = time.monotonic() + timeout_s
        root = spec.output_root / ("helper-" + uuid4().hex)
        root.mkdir(mode=0o700)
        commands = _DockerCommands(spec, root, self.cancelled)
        commands.deadline = deadline
        target = commands.inspect(spec.target.container_id)
        target_image = commands.inspect(target["Image"], image=True)
        validate_container(spec, target, target_image)
        # Main must create and mount this volume before calling prepare. A
        # missing/mismatched volume fails before any helper create command.
        volume = _inspect_target_tmp_volume(commands, spec, target)
        process = _proc_identity(spec.target.process_id)
        if (
            process["uid"] != spec.uid
            or process["gid"] != spec.gid
            or process["ns_pid"] != 1
            or process["pid_namespace"] == str(Path("/proc/self/ns/pid").readlink())
        ):
            raise ValueError(
                "target must be owned init PID in a separate PID namespace"
            )
        image = commands.inspect(spec.helper_image, image=True)
        if (
            spec.helper_image not in (image.get("RepoDigests") or [])
            or image.get("Architecture") != spec.architecture
            or target_image.get("Architecture") != spec.architecture
            or image.get("Os") != "linux"
        ):
            raise ValueError(
                "locally present digest-pinned compatible architecture required"
            )
        name = "nanolab-diag-" + uuid4().hex
        # Persist even before create: a timed-out create might have succeeded.
        journal = root / "ownership.json"
        journal.write_text(
            json.dumps(
                {
                    "target": asdict(spec.target),
                    "name": name,
                    "owner_label": spec.owner_label,
                    "owner_value": spec.owner_value,
                }
            )
        )
        helper_id = ""
        executor = None
        attaching = False
        try:
            helper_id = commands.run(helper_create_argv(spec, name)).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", helper_id):
                raise ValueError("Docker did not return an exact helper ID")
            commands.run(("start", helper_id))
            helper = commands.inspect(helper_id)
            current_volume = _inspect_target_tmp_volume(commands, spec, helper)
            if any(
                current_volume.get(key) != volume.get(key)
                for key in (
                    "Name",
                    "CreatedAt",
                    "Driver",
                    "Scope",
                    "Options",
                    "Labels",
                    "Mountpoint",
                )
            ):
                raise ValueError("owned target tmp volume identity changed")
            host = helper["HostConfig"]
            helper_process = _proc_identity(helper["State"]["Pid"])
            if (
                helper["Image"] != image["Id"]
                or helper["State"].get("Running") is not True
                or host.get("Privileged") is not False
                or host.get("CapAdd")
                or "ALL" not in (host.get("CapDrop") or [])
                or host.get("NetworkMode") != "none"
                or host.get("PidMode") != "container:" + spec.target.container_id
                or host.get("ReadonlyRootfs") is not True
                or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])
                or host.get("IpcMode") != "private"
                or host.get("CgroupnsMode") != "private"
                or host.get("Memory") != spec.helper_memory_bytes
                or (
                    helper_process["pid_namespace"] is not None
                    and process["pid_namespace"] is not None
                    and helper_process["pid_namespace"] != process["pid_namespace"]
                )
                or helper_process["uid"] != spec.uid
                or helper_process["gid"] != spec.gid
            ):
                raise ValueError(
                    "observed helper isolation differs from requested isolation"
                )
            cfg = {
                "target": asdict(spec.target),
                "target_start_ticks": process["start_ticks"],
                "helper_pid": helper_process["ns_pid"],
                "quota_bytes": spec.quota_bytes,
                "node_socket": spec.node_socket,
                "host_output_root": str(spec.output_root),
            }
            bridge = self.assets_dir / "docker-diagnostic-bridge.py"
            python = Path(sys.executable).resolve()
            command = (
                str(python),
                str(bridge),
                _encoded(
                    {
                        "docker": spec.docker,
                        "docker_sha256": _hash(Path(spec.docker)),
                        "docker_host": spec.docker_host,
                        "helper_id": helper_id,
                        "config": cfg,
                    }
                ),
            )
            provisioning_path = root / "provisioning.json"
            # The provisional executor is used ONLY for ownership-checked failure
            # cancellation. No capabilities exist until the real probe succeeds.
            executor = _OwnedDockerHelper(spec, commands, helper_id, process)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("provisioning deadline exhausted")
            attaching = True
            raw = commands.run(
                ("exec", helper_id, PYTHON, WORKER, "probe", _encoded(cfg)), remaining
            )
            probe = json.loads(raw)
            capacity = validate_probe(spec, probe)
            output_stat = spec.output_root.stat()
            if (
                probe["target_start_ticks"] != process["start_ticks"]
                or (
                    process["pid_namespace"] is not None
                    and probe["pid_namespace"] != process["pid_namespace"]
                )
                or (
                    helper_process["mount_namespace"] is not None
                    and probe["mount_namespace"] != helper_process["mount_namespace"]
                )
                or (
                    process["mount_namespace"] is not None
                    and probe["target_mount_namespace"] != process["mount_namespace"]
                )
                or probe["helper_pid"] != helper_process["ns_pid"]
                or probe.get("output_alias") != [output_stat.st_dev, output_stat.st_ino]
            ):
                raise ValueError("worker observations disagree with host procfs")
            # Re-verify the target through the executor that owns it; the
            # check is deliberately not part of its public surface.
            executor._check_target()  # noqa: SLF001
            observations = {
                "target_inspect": target,
                "target_image": target_image,
                "helper_inspect": helper,
                "helper_image": image,
                "probe": probe,
                "host_process": process,
                "helper_process": helper_process,
                "target_tmp_volume": volume,
            }
            (root / "observations.json").write_text(json.dumps(observations, indent=2))
            receipt = {
                "schema": SCHEMA,
                "target": asdict(spec.target),
                "command": list(command),
                "helper_digest": spec.helper_image,
                # The helper's own capability claim, not an echo of the request:
                # the adapter and the runtime gate both read it. Sorted for a
                # stable receipt and for comparison against supported_operations.
                "operations": sorted(supported_operations(spec.target.runtime)),
                "attach_verified": True,
                "runtime_compatible": True,
                "bounded_execution": True,
                "namespace_verified": True,
                "private_control": spec.target.runtime == "node",
                "control_transport": "private-unix"
                if spec.target.runtime == "node"
                else "target-namespace",
                "host_output_root": str(spec.output_root),
                "helper_output_root": str(spec.output_root),
                "output_mode": "framed-stdout",
                "file_output_quota_bytes": capacity,
                "file_output_quota_verified": True,
                "full_gc_source": GC_SOURCES[spec.target.runtime],
                "command_artifacts": [
                    {"path": str(p), "sha256": _hash(p)} for p in (python, bridge)
                ],
                "observations_sha256": _hash(root / "observations.json"),
                "trust": "operator-provisioned; not independent attestation",
                "remote_cancellation": "stop-exact-owned-target-then-remove-helper",
            }
            provisioning_path.write_text(json.dumps(receipt, indent=2))
            executor = LocalDockerDiagnosticExecutor(
                HelperProvisioning(
                    provisioning_path, _hash(provisioning_path), command
                ),
                spec,
                commands,
                helper_id,
                process,
            )
            commands.deadline = None
            return PreparedDockerDiagnostics(
                executor, provisioning_path, helper_id, capacity
            )
        except BaseException as error:
            commands.cleanup_deadline = time.monotonic() + 10.0
            try:
                if attaching and executor is not None:
                    executor.cancel_remote()
                else:
                    # Name was generated and persisted before create. Inspect the
                    # label before cleanup even when the create client timed out.
                    candidate = commands.inspect(helper_id or name, cleanup=True)
                    labels = candidate["Config"].get("Labels") or {}
                    if (
                        labels.get(spec.owner_label) != spec.owner_value
                        or labels.get("nanolab.diagnostic.target")
                        != spec.target.container_id
                    ):
                        raise ValueError("partial helper ownership is unconfirmed")
                    commands.run(("rm", "--force", candidate["Id"]), cleanup=True)
            except BaseException as cleanup_error:
                error.add_note(
                    f"helper provisioning cleanup unconfirmed; journal {journal}: "
                    f"{cleanup_error}"
                )
            raise

    def prepare_memory(self, spec: DockerHelperSpec, *, timeout_s=30.0):
        """Provision a persistent procfs reader without GC, attachment or mounts."""
        if not spec.memory_only or not 0 < timeout_s <= 300:
            raise ValueError(
                "explicit memory_only specification and bounded timeout required"
            )
        root = spec.output_root / ("memory-helper-" + uuid4().hex)
        root.mkdir(mode=0o700)
        commands = _DockerCommands(spec, root, self.cancelled)
        commands.deadline = time.monotonic() + timeout_s
        target = commands.inspect(spec.target.container_id)
        target_image = commands.inspect(target["Image"], image=True)
        validate_container(spec, target, target_image)
        process = _proc_identity(spec.target.process_id)
        if (
            process["uid"] != spec.uid
            or process["gid"] != spec.gid
            or process["ns_pid"] != 1
        ):
            raise ValueError("matching UID/GID container-init process required")
        image = commands.inspect(spec.helper_image, image=True)
        if (
            spec.helper_image not in (image.get("RepoDigests") or [])
            or image.get("Architecture") != spec.architecture
            or target_image.get("Architecture") != spec.architecture
            or image.get("Os") != "linux"
        ):
            raise ValueError("pinned local helper image/architecture required")
        name = "nanolab-memory-" + uuid4().hex
        journal = root / "ownership.json"
        journal.write_text(
            json.dumps(
                {
                    "mode": "memory-only",
                    "name": name,
                    "target": asdict(spec.target),
                    "owner_label": spec.owner_label,
                    "owner_value": spec.owner_value,
                }
            )
        )
        helper_id = ""
        try:
            helper_id = commands.run(helper_create_argv(spec, name)).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", helper_id):
                raise ValueError("exact helper ID required")
            commands.run(("start", helper_id))
            data = commands.inspect(helper_id)
            host = data["HostConfig"]
            holder = _proc_identity(data["State"]["Pid"])
            if (
                data["Image"] != image["Id"]
                or data["State"].get("Running") is not True
                or host.get("Privileged") is not False
                or host.get("CapAdd")
                or "ALL" not in (host.get("CapDrop") or [])
                or host.get("PidMode") != "container:" + spec.target.container_id
                or host.get("NetworkMode") != "none"
                or host.get("ReadonlyRootfs") is not True
                or host.get("Memory") != spec.helper_memory_bytes
                or holder["uid"] != spec.uid
                or holder["gid"] != spec.gid
            ):
                raise ValueError("observed memory helper isolation mismatch")
            owner = _OwnedDockerHelper(spec, commands, helper_id, process)
            assert commands.deadline is not None  # nosec B101 - validated invariant/type narrowing
            remaining = commands.deadline - time.monotonic()
            initial = owner.read_memory(remaining)
            evidence = root / "observations.json"
            evidence.write_text(
                json.dumps(
                    {
                        "target_inspect": target,
                        "helper_inspect": data,
                        "helper_image": image,
                        "initial_sample": initial,
                    },
                    indent=2,
                )
            )
            return PreparedDockerMemory(owner, helper_id, initial, evidence)
        except BaseException as error:
            try:
                commands.cleanup_deadline = time.monotonic() + 5.0
                data = commands.inspect(helper_id or name, cleanup=True)
                labels = data["Config"].get("Labels") or {}
                if (
                    labels.get(spec.owner_label) != spec.owner_value
                    or labels.get("nanolab.diagnostic.target")
                    != spec.target.container_id
                ):
                    raise ValueError("partial memory helper ownership mismatch")
                commands.run(("rm", "--force", data["Id"]), cleanup=True)
            except BaseException as cleanup_error:
                error.add_note(
                    f"memory helper cleanup unconfirmed; journal {journal}: "
                    f"{cleanup_error}"
                )
            raise
