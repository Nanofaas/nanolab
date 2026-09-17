"""Bounded execution of explicitly provisioned diagnostic helpers.

The helper receives one JSON request on stdin. It emits JSONL artifact frames
``{kind: artifact, name, data_base64}`` followed by exactly one ``result`` frame.
Only this executor writes streamed artifacts. The pinned, trusted helper owns
target attachment, private control, namespace translation and remote cancellation.
It must never open a public inspector. File-producing operations additionally
require a provisioned target-side filesystem quota no larger than the request.

Receipt hashes establish which operator-provisioned helper is trusted, not an
independent attestation authority. No helper, namespace mount, socket, container,
or debug endpoint is provisioned here. No command is interpreted by a shell.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import selectors
import stat
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Any

from nanolab.tasks.soak.diagnostics import (
    OPERATION_VOCABULARY,
    DiagnosticCapabilities,
    DiagnosticOutcome,
    DiagnosticRequest,
)
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.processes import OwnedCommandRunner

_SCHEMA = "nanolab-soak-diagnostic-helper-v1"
_FRAME_LIMIT = 256 * 1024
_RECEIPT_LIMIT = 1024 * 1024

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

# This bridge execs the pinned helper inside OwnedCommandRunner's subreaper
# ancestry. FIFOs preserve streaming without giving the helper the supervisor's
# control pipe or trusting its report of local child ownership.
_HELPER_BRIDGE = r"""
import hashlib, json, os, stat, sys
config = json.load(open(sys.argv[1]))
pinned = {}
for path, expected in config['artifacts'].items():
    descriptor = os.open(path, os.O_RDONLY)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise ValueError('helper identity must name a regular file')
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError('helper executable identity changed')
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.set_inheritable(descriptor, True)
    pinned[path] = descriptor
for name, target, flags in (('request', 0, os.O_RDONLY),
                             ('stdout', 1, os.O_WRONLY),
                             ('stderr', 2, os.O_WRONLY)):
    descriptor = os.open(config[name], flags)
    os.dup2(descriptor, target)
    os.close(descriptor)
argv = ['/proc/self/fd/%s' % pinned[arg] if arg in pinned else arg
        for arg in config['command']]
os.execve(argv[0], argv, os.environ)
"""


def _positive(value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("diagnostic timeout must be finite and positive")


def _read_json(path: Path, limit: int = _RECEIPT_LIMIT) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink():
        raise ValueError("symlink diagnostic evidence is unsupported")
    with path.open("rb") as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError("diagnostic evidence exceeds its size budget")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("diagnostic evidence must be a JSON object")
    return value, body


@dataclass(frozen=True)
class HelperProvisioning:
    """Explicit receipt hash and argv supplied by the provisioning owner."""

    receipt: Path
    receipt_sha256: str
    command: tuple[str, ...]


class ProvisionedDiagnosticExecutor:
    """Implement DiagnosticExecutor for one exact provisioned process identity."""

    def __init__(self, provisioning: HelperProvisioning):
        """Verify the provisioned helper and bind its exact process identity."""
        self.provisioning = provisioning
        self._receipt = self._verify_receipt()
        self._target = Target(**self._receipt["target"])
        self._root = Path(self._receipt["host_output_root"])
        self._command_artifacts = {
            item["path"]: item["sha256"] for item in self._receipt["command_artifacts"]
        }
        # Verify executable identities at construction as well as on every call.
        with ExitStack() as stack:
            self._pinned_command(stack)

    def _verify_receipt(self) -> dict[str, Any]:
        config = self.provisioning
        receipt, body = _read_json(config.receipt)
        if hashlib.sha256(body).hexdigest() != config.receipt_sha256:
            raise ValueError("provisioning receipt identity changed")
        if receipt.get("schema") != _SCHEMA or receipt.get("command") != list(
            config.command
        ):
            raise ValueError("unsupported helper schema or command identity")
        if (
            not config.command
            or not all(
                isinstance(arg, str) and arg and "\0" not in arg
                for arg in config.command
            )
            or not Path(config.command[0]).is_absolute()
            or Path(config.command[0]).name in {"sh", "bash", "dash", "zsh", "ksh"}
        ):
            raise ValueError(
                "explicit absolute argv helper required; shells are unsupported"
            )
        target = Target(**receipt["target"])
        if not re.fullmatch(
            r"[^\s@]+@sha256:[0-9a-f]{64}", receipt.get("helper_digest", "")
        ):
            raise ValueError("immutable provisioned helper digest required")
        for field in (
            "attach_verified",
            "runtime_compatible",
            "bounded_execution",
            "namespace_verified",
        ):
            if receipt.get(field) is not True:
                raise ValueError(f"unavailable helper capability: {field}")
        operations = receipt.get("operations")
        if (
            not isinstance(operations, list)
            or not operations
            or any(not isinstance(op, str) for op in operations)
            or len(set(operations)) != len(operations)
            or not set(operations).issubset(OPERATION_VOCABULARY)
        ):
            raise ValueError("unsupported provisioned diagnostic operations")
        root = receipt.get("host_output_root")
        if (
            not isinstance(root, str)
            or not Path(root).is_absolute()
            or Path(root).is_symlink()
            or not Path(root).is_dir()
            or receipt.get("helper_output_root") != root
            or receipt.get("output_mode") != "framed-stdout"
        ):
            raise ValueError(
                "verified matching output namespace and framed transport required"
            )
        if target.runtime == "node":
            if (
                receipt.get("private_control") is not True
                or receipt.get("control_transport")
                not in {"private-pipe", "private-unix"}
                or not set(operations).issubset({"gc", "heap_dump"})
            ):
                raise ValueError("Node diagnostics require provisioned private control")
        elif receipt.get("control_transport") != "target-namespace":
            raise ValueError("target namespace control transport required")
        artifacts = receipt.get("command_artifacts")
        if not isinstance(artifacts, list) or not artifacts or len(artifacts) > 32:
            raise ValueError("helper executable identity artifacts required")
        paths = set()
        for item in artifacts:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or not Path(item["path"]).is_absolute()
                or item["path"] in paths
                or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))
            ):
                raise ValueError("invalid helper command artifact identity")
            paths.add(item["path"])
        if any(arg.startswith("/") and arg not in paths for arg in config.command):
            raise ValueError("every command file must have a pinned identity")
        return receipt

    def _pinned_command(
        self, stack: ExitStack
    ) -> tuple[tuple[str, ...], tuple[int, ...]]:
        """Hash and execute the same open files, preventing replacement races."""
        pinned: dict[str, int] = {}
        for path, expected in self._command_artifacts.items():
            descriptor = os.open(path, os.O_RDONLY)
            stack.callback(os.close, descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("helper identity must name a regular file")
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 65536):
                digest.update(chunk)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if digest.hexdigest() != expected:
                raise ValueError("helper executable identity changed")
            pinned[path] = descriptor
        argv = tuple(
            f"/proc/self/fd/{pinned[arg]}" if arg in pinned else arg
            for arg in self.provisioning.command
        )
        return argv, tuple(pinned.values())

    def capabilities(self) -> DiagnosticCapabilities:
        """Return verified operations, excluding file output without a quota."""
        receipt = self._verify_receipt()
        operations = frozenset(receipt["operations"])
        quota = receipt.get("file_output_quota_bytes")
        if (
            receipt.get("file_output_quota_verified") is not True
            or type(quota) is not int
            or quota <= 0
        ):
            operations -= {"heap_dump", "jfr"}
        return DiagnosticCapabilities(
            self._target,
            receipt["helper_digest"],
            operations,
            self.provisioning.receipt,
            True,
            True,
            True,
            receipt.get("private_control") is True,
        )

    def _exchange(
        self,
        message: dict[str, Any],
        timeout_s: float,
        *,
        output: Path | None = None,
        max_bytes: int = 0,
    ) -> dict[str, Any]:
        _positive(timeout_s)
        deadline = time.monotonic() + timeout_s
        self._verify_receipt()
        payload = (json.dumps(message, allow_nan=False) + "\n").encode()
        if len(payload) > 65536:
            raise ValueError("helper request exceeds its size budget")
        result: dict[str, Any] | None = None
        wire_bytes = 0
        artifact_bytes = 0
        files: dict[str, Any] = {}
        pending = bytearray()
        stderr = bytearray()

        def frame(body: bytes, stack: ExitStack) -> None:
            nonlocal result, artifact_bytes
            value = json.loads(body)
            if not isinstance(value, dict) or result is not None:
                raise ValueError("invalid helper frame or output after completion")
            if value.get("kind") == "result":
                result = value
                return
            if value.get("kind") != "artifact" or output is None:
                raise ValueError("unsupported helper artifact frame")
            name = value.get("name")
            if not isinstance(name, str) or _NAME.fullmatch(name) is None:
                raise ValueError("unsafe diagnostic artifact name")
            encoded = value.get("data_base64")
            if not isinstance(encoded, str):
                raise ValueError("invalid diagnostic artifact encoding")
            chunk = base64.b64decode(encoded, validate=True)
            if artifact_bytes + len(chunk) > max_bytes:
                raise OSError("diagnostic artifact byte budget exhausted")
            if name not in files:
                if len(files) >= 32:
                    raise OSError("diagnostic artifact count budget exhausted")
                descriptor = os.open(
                    output / name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                files[name] = stack.enter_context(
                    os.fdopen(descriptor, "wb", buffering=0)
                )
            remaining = memoryview(chunk)
            while remaining:
                written = files[name].write(remaining)
                if not written:
                    raise OSError("diagnostic artifact write made no progress")
                artifact_bytes += written
                remaining = remaining[written:]

        with ExitStack() as stack:
            # Check before starting a supervisor, and pin again in the bridge
            # immediately before exec so replacement races cannot select code.
            self._pinned_command(stack)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("PYTHON", "LD_", "DYLD_"))
            }
            scratch = Path(
                stack.enter_context(
                    TemporaryDirectory(prefix=".diagnostic-supervisor-", dir=self._root)
                )
            )
            request_file = scratch / "request.json"
            request_file.write_bytes(payload)
            config_file = scratch / "bridge.json"
            config_file.write_text(
                json.dumps(
                    {
                        "command": self.provisioning.command,
                        "artifacts": self._command_artifacts,
                        "request": str(request_file),
                        "stdout": str(scratch / "stdout"),
                        "stderr": str(scratch / "stderr"),
                    }
                )
            )
            selector = stack.enter_context(selectors.DefaultSelector())
            for label in ("stdout", "stderr"):
                path = scratch / label
                os.mkfifo(path, 0o600)
                # Keep a local writer open until the supervisor is finished;
                # otherwise an early FIFO EOF could masquerade as completion.
                descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                stack.callback(os.close, descriptor)
                selector.register(descriptor, selectors.EVENT_READ, label)
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                raise TimeoutError("diagnostic helper deadline exceeded before launch")
            cancelled = Event()
            runner = OwnedCommandRunner(
                (sys.executable, "-c", _HELPER_BRIDGE, str(config_file)),
                cwd=scratch,
                env=env,
                log_path=scratch / "supervisor.log",
                timeout_s=remaining_s,
                cancelled=cancelled,
                output_limit_bytes=8192,
                stop_timeout_s=1.0,
            )
            supervision: dict[str, Any] = {}

            def supervise() -> None:
                try:
                    supervision["result"] = runner.run()
                except BaseException as error:
                    supervision["error"] = error

            worker = Thread(
                target=supervise, name="diagnostic-helper-owner", daemon=True
            )
            worker.start()
            primary_error: BaseException | None = None
            try:
                while True:
                    remaining_s = deadline - time.monotonic()
                    alive = worker.is_alive()
                    if alive and remaining_s <= 0:
                        raise TimeoutError("diagnostic helper deadline exceeded")
                    ready = selector.select(min(0.05, remaining_s) if alive else 0)
                    if not alive and not ready:
                        break
                    for key, _events in ready:
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            continue
                        wire_bytes += len(chunk)
                        if wire_bytes > 2 * max_bytes + 65536:
                            raise OSError(
                                "diagnostic helper wire output budget exhausted"
                            )
                        if key.data == "stderr":
                            if len(stderr) + len(chunk) > 8192:
                                raise OSError(
                                    "diagnostic helper stderr budget exhausted"
                                )
                            stderr.extend(chunk)
                            continue
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if len(line) > _FRAME_LIMIT:
                                raise OSError(
                                    "diagnostic helper frame budget exhausted"
                                )
                            frame(bytes(line), stack)
                        if len(pending) > _FRAME_LIMIT:
                            raise OSError("diagnostic helper frame budget exhausted")
                if pending:
                    raise ValueError("incomplete diagnostic helper frame")
                if "error" in supervision:
                    raise OSError(
                        "diagnostic local supervision failed"
                    ) from supervision["error"]
                owned = supervision["result"]
                if not owned.reaped or owned.ended_s is None:
                    raise OSError("diagnostic local descendants were not fully reaped")
                if owned.timed_out:
                    raise TimeoutError("diagnostic helper deadline exceeded")
                if (
                    owned.cancelled
                    or owned.quota_exceeded
                    or owned.forced_stop
                    or owned.errors
                ):
                    raise OSError(
                        "diagnostic helper required forced descendant cleanup "
                        "or supervision failed"
                    )
                if owned.returncode != 0:
                    raise OSError(
                        f"diagnostic helper exited {owned.returncode}: "
                        f"{stderr.decode('utf-8', 'replace')[:1024]}"
                    )
                if result is None:
                    raise ValueError("diagnostic helper completion frame missing")
                result["_local_descendants_reaped"] = True
                if message.get("kind") == "execute" and (
                    result.get("completed") is False
                    or result.get("remote_finished") is False
                    or (
                        type(result.get("exit_code")) is int
                        and result["exit_code"] != 0
                    )
                ):
                    # Persist only an identity-checked, bounded failure summary.
                    # Raising here still enters the Docker owner's cancellation
                    # path; a failed remote operation never becomes a success.
                    if (
                        result.get("target") != asdict(self._target)
                        or message.get("target") != asdict(self._target)
                        or (
                            "request_id" in result
                            and result["request_id"] != message.get("request_id")
                        )
                    ):
                        raise ValueError("diagnostic failure result identity changed")
                    remote_error = result.get("error")
                    detail = (
                        remote_error[:2048]
                        if isinstance(remote_error, str)
                        else "remote operation reported failure without a text error"
                    )
                    failure = {
                        "schema": "nanolab-soak-diagnostic-failure-v1",
                        "kind": "remote_failure",
                        "target": asdict(self._target),
                        "request_id": message.get("request_id"),
                        "operation": message.get("operation"),
                        "error": detail,
                        "error_truncated": isinstance(remote_error, str)
                        and len(remote_error) > 2048,
                        "_local_descendants_reaped": True,
                    }
                    for field in ("completed", "remote_finished", "descendants_reaped"):
                        if type(result.get(field)) is bool:
                            failure[field] = result[field]
                    code = result.get("exit_code")
                    if type(code) is int and -(2**31) <= code < 2**31:
                        failure["exit_code"] = code
                    error = RuntimeError(f"remote diagnostic failed: {detail}")
                    try:
                        body = (json.dumps(failure, allow_nan=False) + "\n").encode()
                        if output is None or len(body) > min(
                            16384, max_bytes - artifact_bytes
                        ):
                            raise OSError(
                                "failure evidence exceeds remaining output budget"
                            )
                        descriptor = os.open(
                            output / "remote-failure.json",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                        )
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(body)
                    except (OSError, ValueError, TypeError) as evidence_error:
                        error.add_note(
                            f"remote failure evidence not saved: {evidence_error}"
                        )
                    raise error
                return result
            except BaseException as error:
                primary_error = error
                raise
            finally:
                if worker.is_alive():
                    try:
                        cancelled.set()
                        runner.stop(1.0)
                        worker.join(1.0)
                        if worker.is_alive():
                            raise TimeoutError(
                                "diagnostic local supervisor did not finish"
                            )
                        owned = supervision.get("result")
                        if owned is None or not owned.reaped:
                            raise OSError(
                                "diagnostic local descendant cleanup is unconfirmed"
                            )
                    except BaseException as cleanup_error:
                        if primary_error is None:
                            raise
                        primary_error.add_note(
                            f"owned diagnostic cleanup: {cleanup_error}"
                        )

    def inspect(self, target: Target, timeout_s: float) -> Target:
        """Check that the provisioned process remains running with its identity."""
        if target != self._target:
            raise ValueError("target differs from provisioned process identity")
        result = self._exchange(
            {"schema": _SCHEMA, "kind": "inspect", "target": asdict(target)}, timeout_s
        )
        if result.get("target") != asdict(target) or result.get("running") is not True:
            raise ValueError(
                "diagnostic target identity changed or process unavailable"
            )
        return target

    def execute(self, request: DiagnosticRequest) -> DiagnosticOutcome:
        """Collect bounded artifacts and verify operation completion evidence."""
        _positive(request.timeout_s)
        deadline = time.monotonic() + request.timeout_s
        receipt = self._verify_receipt()
        if (
            request.target != self._target
            or request.operation not in receipt["operations"]
        ):
            raise ValueError("diagnostic operation or target is not provisioned")
        if type(request.max_bytes) is not int or request.max_bytes <= 0:
            raise ValueError("positive diagnostic output budget required")
        output = request.output_dir
        if (
            output.is_symlink()
            or not output.is_dir()
            or not output.resolve().is_relative_to(self._root.resolve())
            or any(output.iterdir())
        ):
            raise ValueError(
                "diagnostic artifacts require a fresh owned output directory"
            )
        if request.operation in {"heap_dump", "jfr"}:
            quota = receipt.get("file_output_quota_bytes")
            if (
                receipt.get("file_output_quota_verified") is not True
                or type(quota) is not int
                or not 0 < quota <= request.max_bytes
            ):
                raise ValueError(
                    "file diagnostics require verified target-side output quota"
                )
        node_methods = {
            "gc": "HeapProfiler.collectGarbage",
            "heap_dump": "HeapProfiler.takeHeapSnapshot",
        }
        if (
            request.target.runtime == "node"
            and request.protocol_method != node_methods.get(request.operation)
        ):
            raise ValueError("unprovisioned private inspector method")

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("diagnostic operation deadline exceeded")
            return value

        self.inspect(request.target, remaining())
        message = {
            "schema": _SCHEMA,
            "kind": "execute",
            **asdict(request),
            "output_dir": str(output),
            "timeout_s": remaining(),
        }
        result = self._exchange(
            message, remaining(), output=output, max_bytes=request.max_bytes
        )
        if result.get("target") != asdict(request.target):
            raise ValueError("diagnostic completion target identity changed")
        self.inspect(request.target, remaining())
        code = result.get("exit_code")
        if type(code) is not int:
            code = None
        completed = (
            code == 0
            and result.get("completed") is True
            and result.get("descendants_reaped") is True
            and result.get("_local_descendants_reaped") is True
        )
        before, after = result.get("before_count"), result.get("after_count")
        before = before if type(before) is int and before >= 0 else None
        after = after if type(after) is int and after >= 0 else None
        event_name = None
        if request.operation == "gc":
            candidate = result.get("full_gc_event")
            verified = False
            if isinstance(candidate, str) and _NAME.fullmatch(candidate):
                try:
                    event, _ = _read_json(output / candidate)
                    start, end = event.get("started_s"), event.get("ended_s")
                    verified = (
                        event.get("schema") == "nanolab-soak-v1"
                        and event.get("kind") == "full_gc_completed"
                        and event.get("target") == asdict(request.target)
                        and event.get("request_id") == request.request_id
                        and bool(receipt.get("full_gc_source"))
                        and event.get("source") == receipt["full_gc_source"]
                        and (type(start) is int or type(start) is float)
                        and (type(end) is int or type(end) is float)
                        and math.isfinite(start)
                        and math.isfinite(end)
                        and request.started_s <= start <= end <= time.monotonic()
                        and before is not None
                        and after is not None
                        and after > before
                    )
                except (OSError, ValueError, TypeError):
                    verified = False
                if verified:
                    event_name = candidate
            completed = completed and verified
        return DiagnosticOutcome(code, completed, before, after, event_name)
