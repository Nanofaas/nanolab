"""Evidence-gated runtime diagnostics; no helper is provisioned implicitly.

Executors are explicit integration ports, not a default shell escape. Their
preflight must prove compatible attachment, PID/mount namespace access, bounded
execution/output, and separate helper accounting. A command timeout does not
prove that a diagnostic operation inside the target has stopped.
"""

import json
import math
import re
import shutil
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, TypeGuard
from uuid import uuid4

from nanolab.tasks.soak.artifacts import ArtifactWriter, describe_artifact
from nanolab.tasks.soak.models import Availability, CriterionResult, Target

_RECEIPT_BYTES = 65536
# The operation vocabulary: every name this system knows, in any runtime.
# Membership means "a scenario may declare this name", not "a run can execute
# it" -- LOCAL_HELPER_OPERATIONS is the latter.
OPERATION_VOCABULARY = frozenset(
    (
        "gc",
        "histogram",
        "heap_dump",
        "jfr",
        "native_memory",
        "native_memory_baseline",
        "native_memory_diff",
    )
)
# What the pinned local helper can actually dispatch, per runtime, and the one
# declaration the provisioner receipt, the runtime gate and the worker all
# answer to. Five independent copies of this set is how `histogram` and `jfr`
# came to be names the type system accepted while no run path could reach them;
# every copy lives here now.
#
# `jfr` is deliberately absent: the adapter names a recording and the worker
# dumps one for the full-GC probe, but no request path reaches a JFR capture, so
# advertising it here would provision a run that fails at its first capture.
# `histogram` and `native_memory` are text readings of the target JVM.
LOCAL_HELPER_OPERATIONS: Mapping[str, frozenset[str]] = {
    "jvm": frozenset(
        (
            "gc",
            "histogram",
            "heap_dump",
            "native_memory",
            "native_memory_baseline",
            "native_memory_diff",
        )
    ),
    "node": frozenset(("gc", "heap_dump")),
}


def supported_operations(runtime: str) -> frozenset[str]:
    """Return the operations the local helper dispatches, if it supports any."""
    return LOCAL_HELPER_OPERATIONS.get(runtime, frozenset())


def gc_completed(
    before_count: int | None, after_count: int | None, requested_ok: bool
) -> bool:
    """Require a matching full-cycle event: a counter increase is not one."""
    return (
        requested_ok is True
        and type(before_count) is int
        and type(after_count) is int
        and 0 <= before_count < after_count
    )


def _finite(value: object, *, positive: bool = False) -> TypeGuard[float]:
    try:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and (value > 0 if positive else value >= 0)
        )
    except OverflowError:
        return False


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        body = stream.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("diagnostic JSON limit exceeded")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("diagnostic JSON must be an object")
    return value


def _safe_file(root: Path, relative: str) -> Path:
    name = Path(relative)
    if root.is_symlink() or name.is_absolute() or not name.parts or ".." in name.parts:
        raise ValueError("artifact must remain inside its evidence root")
    path = root
    for part in name.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink evidence is unsupported")
    if not path.is_file():
        raise ValueError("artifact is not a regular file")
    return path


def _verify_artifact(record: object, root: Path) -> Path:
    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
        raise ValueError("missing artifact descriptor")
    path = _safe_file(root, record["path"])
    observed = describe_artifact(path)
    if (
        type(record.get("size_bytes")) is not int
        or observed["size_bytes"] != record["size_bytes"]
        or observed["sha256"] != record.get("sha256")
    ):
        raise ValueError("artifact size/hash mismatch")
    return path


@dataclass(frozen=True)
class DiagnosticCapabilities:
    """Observed evidence that a target can be attached to and diagnosed."""

    target: Target
    helper_digest: str
    operations: frozenset[str]
    evidence: Path
    attach_verified: bool
    runtime_compatible: bool
    bounded_execution: bool
    private_control: bool


@dataclass(frozen=True)
class DiagnosticRequest:
    """One identified diagnostic operation against one owned target."""

    request_id: str
    target: Target
    operation: str
    argv: tuple[str, ...]
    protocol_method: str | None
    output_dir: Path
    max_bytes: int
    timeout_s: float
    started_s: float


@dataclass(frozen=True)
class DiagnosticOutcome:
    """What a diagnostic execution actually did, including a timed-out one."""

    exit_code: int | None
    completed: bool
    before_count: int | None
    after_count: int | None
    full_gc_event: str | None


class DiagnosticExecutor(Protocol):
    """Provisioned executor; all calls must return within their given budget.

    execute must enforce max_bytes at the writer, return only after artifact
    writers finish, and preserve partial files on failure. JVM file output paths
    must be mapped into the target namespace at the same absolute path. Node
    commands require an already-provisioned private inspector connection.
    """

    def inspect(self, target: Target, timeout_s: float) -> Target:
        """Return the target's identity as observed now, for before/after checks."""
        ...

    def execute(self, request: DiagnosticRequest) -> DiagnosticOutcome:
        """Run one bounded diagnostic operation against the target."""
        ...


class DiagnosticBudget:
    """Shared, conservative reservations across roles/checkpoints.

    Reservations, including failed attempts, are not refunded. This bounds later
    captures even when a timed-out target may still be writing a partial dump.
    """

    def __init__(self, max_dumps: int, max_dump_bytes: int, max_artifact_bytes: int):
        """Fix the dump count and byte ceilings every capture reserves from."""
        if any(
            type(value) is not int or value < 0
            for value in (max_dumps, max_dump_bytes, max_artifact_bytes)
        ):
            raise ValueError("diagnostic budgets must be nonnegative integers")
        self._dumps = max_dumps
        self._dump_bytes = max_dump_bytes
        self._artifact_bytes = max_artifact_bytes
        self._lock = Lock()

    def reserve(self, max_bytes: int, *, dump: bool) -> None:
        """Reserve a capture's worst-case bytes before it runs, never after."""
        with self._lock:
            if self._artifact_bytes < max_bytes + _RECEIPT_BYTES:
                raise ValueError("diagnostic artifact budget exhausted")
            if dump and (self._dumps < 1 or self._dump_bytes < max_bytes):
                raise ValueError("diagnostic dump budget exhausted")
            self._artifact_bytes -= max_bytes + _RECEIPT_BYTES
            if dump:
                self._dumps -= 1
                self._dump_bytes -= max_bytes


class _RuntimeAdapter:
    runtime = ""
    supported: frozenset[str] = frozenset()

    def __init__(
        self,
        capabilities: DiagnosticCapabilities,
        executor: DiagnosticExecutor,
        budget: DiagnosticBudget,
        *,
        natural_checkpoint: Path,
        max_capture_bytes: int,
        full_gc_source: str,
        natural_phase: str = "drain",
    ):
        if type(max_capture_bytes) is not int or max_capture_bytes <= 0:
            raise ValueError("positive diagnostic capture limit required")
        if not natural_phase:
            raise ValueError("the declared natural checkpoint phase cannot be empty")
        if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", capabilities.helper_digest):
            raise ValueError("immutable diagnostic helper digest required")
        if capabilities.evidence.is_symlink():
            raise ValueError("symlink capability evidence is unsupported")
        self._capabilities = capabilities
        self._capability_artifact = describe_artifact(capabilities.evidence)
        self._executor = executor
        self._budget = budget
        self._natural_checkpoint = natural_checkpoint
        self._natural_phase = natural_phase
        self._max_bytes = max_capture_bytes
        self._full_gc_source = full_gc_source

    def capabilities(self, target: Target) -> frozenset[str]:
        caps = self._capabilities
        if (
            target != caps.target
            or target.runtime != self.runtime
            or caps.attach_verified is not True
            or caps.runtime_compatible is not True
            or caps.bounded_execution is not True
            or (self.runtime == "node" and caps.private_control is not True)
        ):
            return frozenset()
        return frozenset(caps.operations & self.supported)

    def _command(
        self, target: Target, operation: str, output: Path
    ) -> tuple[tuple[str, ...], str | None]:
        raise NotImplementedError

    def _natural(self, target: Target, started: float) -> dict[str, object]:
        # The checkpoint must name the phase this adapter was built for: soak's
        # drain by default, so a capture can never accept a checkpoint taken in
        # some other phase. A caller that measures a different unperturbed
        # window declares it instead of loosening the gate.
        path = self._natural_checkpoint
        if path.is_symlink():
            raise ValueError("symlink natural checkpoint is unsupported")
        receipt = _read_json(path)
        if (
            receipt.get("schema") != "nanolab-soak-v1"
            or receipt.get("kind") != "natural_checkpoint"
            or receipt.get("completed") is not True
            or receipt.get("phase") != self._natural_phase
            or receipt.get("target") != asdict(target)
            or not _finite(receipt.get("ended_s"))
            or receipt["ended_s"] > started
        ):
            raise ValueError(
                "completed natural final checkpoint required before diagnostics"
            )
        artifacts = receipt.get("artifacts")
        if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 32:
            raise ValueError("natural checkpoint artifacts missing or excessive")
        for artifact in artifacts:
            _verify_artifact(artifact, path.parent)
        return describe_artifact(path)

    def _artifacts(self, output: Path) -> list[dict[str, object]]:
        artifacts = []
        total = 0
        for path in output.iterdir():
            path = _safe_file(output, path.name)
            total += path.stat().st_size
            if total > self._max_bytes or len(artifacts) >= 32:
                raise ValueError("diagnostic output exceeded reserved limit")
            descriptor = describe_artifact(path)
            descriptor["path"] = str(Path("artifacts") / path.name)
            artifacts.append(descriptor)
        return sorted(artifacts, key=lambda item: str(item["path"]))

    def _full_gc(
        self, request: DiagnosticRequest, outcome: DiagnosticOutcome, ended: float
    ) -> bool:
        if (
            not gc_completed(
                outcome.before_count,
                outcome.after_count,
                outcome.exit_code == 0 and outcome.completed is True,
            )
            or not outcome.full_gc_event
            or not self._full_gc_source
        ):
            return False
        event = _read_json(_safe_file(request.output_dir, outcome.full_gc_event))
        return (
            event.get("schema") == "nanolab-soak-v1"
            and event.get("kind") == "full_gc_completed"
            and event.get("request_id") == request.request_id
            and event.get("target") == asdict(request.target)
            and event.get("source") == self._full_gc_source
            and _finite(event.get("started_s"))
            and _finite(event.get("ended_s"))
            and request.started_s <= event["started_s"] <= event["ended_s"] <= ended
        )

    def capture(
        self, target: Target, checkpoint: str, output_dir: Path, timeout_s: float
    ) -> Path:
        """Capture one operation into a new owned directory, returning its receipt.

        checkpoint names an operation; the unique request ID distinguishes repeat
        checkpoints. Receipt PASS means diagnostic completion only, never soak PASS.
        """
        if not _finite(timeout_s, positive=True):
            raise ValueError("positive finite diagnostic timeout required")
        writer = ArtifactWriter(output_dir, _RECEIPT_BYTES)
        output = output_dir / "artifacts"
        output.mkdir()
        started = time.monotonic()
        deadline = started + timeout_s
        request_id = uuid4().hex
        receipt: dict[str, Any] = {
            "schema": "nanolab-soak-v1",
            "scope": "diagnostic-only",
            "request_id": request_id,
            "operation": checkpoint,
            "target": asdict(target),
            "started_s": started,
            "helper_digest": self._capabilities.helper_digest,
            "capability_artifact": self._capability_artifact,
            "status": "INCONCLUSIVE",
            "availability": "unavailable",
            "reason": "not completed",
            "full_gc_verified": False,
            "command_completed": False,
            "exit_code": None,
            "artifacts": [],
            "perturbation": {
                "started_s": None,
                "ended_s": None,
                "exclude_from_natural_windows": True,
            },
        }
        interrupted: BaseException | None = None

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("diagnostic operation deadline exceeded")
            return value

        try:
            writer.append(
                "events",
                {
                    "kind": "diagnostic_preflight",
                    "request_id": request_id,
                    "target": asdict(target),
                    "at_s": started,
                },
            )
            if (
                checkpoint not in OPERATION_VOCABULARY
                or checkpoint not in self.capabilities(target)
            ):
                raise ValueError(
                    "required diagnostic capability unavailable for this runtime/target"
                )
            if (
                describe_artifact(self._capabilities.evidence)
                != self._capability_artifact
            ):
                raise ValueError("capability evidence changed")
            receipt["natural_checkpoint"] = self._natural(target, started)
            if self._executor.inspect(target, remaining()) != target:
                raise ValueError(
                    "wrong PID or target identity before diagnostic command"
                )
            if shutil.disk_usage(output_dir).free < self._max_bytes + _RECEIPT_BYTES:
                raise ValueError("insufficient free disk for diagnostic reservation")
            self._budget.reserve(self._max_bytes, dump=checkpoint == "heap_dump")
            argv, method = self._command(target, checkpoint, output)
            command_started = time.monotonic()
            request = DiagnosticRequest(
                request_id,
                target,
                checkpoint,
                argv,
                method,
                output,
                self._max_bytes,
                remaining(),
                command_started,
            )
            receipt["command"] = {"argv": list(argv), "protocol_method": method}
            receipt["perturbation"]["started_s"] = command_started
            writer.append(
                "events",
                {
                    "kind": "diagnostic_start",
                    "request_id": request_id,
                    "at_s": command_started,
                    "exclude_from_natural_windows": True,
                },
            )
            outcome = self._executor.execute(request)
            receipt.update(
                exit_code=outcome.exit_code,
                command_completed=outcome.completed,
                before_count=outcome.before_count,
                after_count=outcome.after_count,
            )
            if self._executor.inspect(target, remaining()) != target:
                raise ValueError("target identity changed during diagnostic command")
            remaining()
            receipt["artifacts"] = self._artifacts(output)
            if (
                type(outcome.exit_code) is not int
                or outcome.exit_code != 0
                or outcome.completed is not True
            ):
                raise ValueError("diagnostic command failed or completion unverified")
            if checkpoint == "gc":
                receipt["full_gc_verified"] = self._full_gc(
                    request, outcome, time.monotonic()
                )
                if not receipt["full_gc_verified"]:
                    raise ValueError(
                        "requested full GC has no matching completion evidence"
                    )
            elif not receipt["artifacts"] or not any(
                item["size_bytes"] for item in receipt["artifacts"]
            ):
                raise ValueError("diagnostic command produced no nonempty artifact")
            receipt.update(
                status="PASS",
                availability="observed",
                reason="diagnostic completion verified",
            )
            receipt["perturbation"]["ended_s"] = time.monotonic()
        except BaseException as error:
            receipt["reason"] = f"{type(error).__name__}: {str(error)[:1024]}"
            if not isinstance(error, Exception):
                receipt["status"] = "ABORTED"
                interrupted = error
            # Preserve partial artifacts without implying that a
            # timed-out writer stopped.
            try:
                receipt["artifacts"] = self._artifacts(output)
            except (OSError, ValueError) as artifact_error:
                receipt["artifact_error"] = str(artifact_error)[:1024]
        finally:
            receipt["ended_s"] = time.monotonic()
            try:
                path = writer.write_json("diagnostic.json", receipt)
            finally:
                writer.close()
        if interrupted is not None:
            raise interrupted
        return path


class JvmDiagnosticAdapter(_RuntimeAdapter):
    """Drive jcmd against a JVM target with an explicit executable."""

    runtime = "jvm"
    supported = OPERATION_VOCABULARY

    def __init__(
        self,
        *args,
        command_prefix: tuple[str, ...],
        recording_name: str | None = None,
        **kwargs,
    ):
        """Bind the jcmd executable, and the JFR recording name when there is one."""
        if not command_prefix or any(not part for part in command_prefix):
            raise ValueError("explicit compatible jcmd executable required")
        super().__init__(*args, **kwargs)
        self._command_prefix = command_prefix
        self._recording_name = recording_name

    def capabilities(self, target: Target) -> frozenset[str]:
        """Advertise JFR only when an existing recording was actually named."""
        operations = super().capabilities(target)
        return operations if self._recording_name else operations - {"jfr"}

    def _command(
        self, target: Target, operation: str, output: Path
    ) -> tuple[tuple[str, ...], str | None]:
        commands = {
            "gc": ("GC.run",),
            "histogram": ("GC.class_histogram",),
            # `summary`, not the default: a bare VM.native_memory prints the
            # same summary, but naming the mode keeps the recorded argv equal to
            # what the worker dispatches, and `summary` is what reads without a
            # baseline.
            "native_memory": ("VM.native_memory", "summary"),
            # The mark the diff is measured from lives inside the target JVM, so
            # this one is only useful taken at the baseline checkpoint and the
            # diff only meaningful in the same incarnation -- which the target's
            # process_started_at pins for the whole run.
            "native_memory_baseline": ("VM.native_memory", "baseline"),
            "native_memory_diff": ("VM.native_memory", "summary.diff"),
            "heap_dump": ("GC.heap_dump", str(output / "capture.hprof")),
            "jfr": (
                "JFR.dump",
                f"name={self._recording_name}",
                f"filename={output / 'capture.jfr'}",
            ),
        }
        return (
            *self._command_prefix,
            str(target.process_id),
            *commands[operation],
        ), None


class NodeDiagnosticAdapter(_RuntimeAdapter):
    """Drive the Node inspector protocol over an already private connection."""

    runtime = "node"
    supported = frozenset(("gc", "heap_dump"))

    def _command(
        self, target: Target, operation: str, output: Path
    ) -> tuple[tuple[str, ...], str | None]:
        return (), {
            "gc": "HeapProfiler.collectGarbage",
            "heap_dump": "HeapProfiler.takeHeapSnapshot",
        }[operation]


class NativeDiagnosticAdapter(_RuntimeAdapter):
    """Offer only the operations an operator explicitly configured.

    A native image has no jcmd, so nothing is inferred for it.
    """

    runtime = "native"

    def __init__(
        self, *args, commands: Mapping[str, tuple[str, ...]] | None = None, **kwargs
    ):
        """Accept the explicitly configured commands, and advertise only those."""
        super().__init__(*args, **kwargs)
        self._commands = dict(commands or {})
        if any(
            name not in OPERATION_VOCABULARY
            or not argv
            or any(not part for part in argv)
            for name, argv in self._commands.items()
        ):
            raise ValueError("invalid explicitly supported native diagnostic command")
        self.supported = frozenset(self._commands)

    def _command(
        self, target: Target, operation: str, output: Path
    ) -> tuple[tuple[str, ...], str | None]:
        return tuple(
            part.format(pid=target.process_id, output=str(output))
            for part in self._commands[operation]
        ), None

    @staticmethod
    def metric_availability(metric: str) -> Availability:
        """Report JVM metrics as not applicable here, never as zero."""
        return "not_applicable" if metric.startswith("jvm_") else "unavailable"


def validate_attribution(
    record: dict[str, Any], artifact_root: Path
) -> CriterionResult:
    """Validate an attribution only; never replace independent numerical verdicts.

    The evaluator must separately match policy_artifact to the frozen run policy.
    A reviewer-supplied population budget is not permission to change that policy.
    """
    criterion = str(record.get("criterion_id", "attribution"))
    evidence: tuple[str, ...] = ()
    try:
        expected = {
            "schema",
            "criterion_id",
            "status",
            "owner",
            "population",
            "expected_lifetime_s",
            "remaining_bytes",
            "budget_bytes",
            "rationale",
            "reviewer",
            "policy_override",
            "policy_artifact",
            "artifacts",
        }
        if set(record) != expected or record["schema"] != "nanolab-soak-v1":
            raise ValueError("invalid attribution schema")
        for key in ("criterion_id", "owner", "population", "rationale", "reviewer"):
            if not isinstance(record[key], str) or not record[key].strip():
                raise ValueError(f"missing attribution {key}")
        if record["policy_override"] is not False:
            raise ValueError("attribution cannot override policy")
        if not _finite(record["expected_lifetime_s"]):
            raise ValueError("invalid expected population lifetime")
        for key in ("remaining_bytes", "budget_bytes"):
            if type(record[key]) is not int or record[key] < 0:
                raise ValueError(f"invalid attribution {key}")
        artifacts = record["artifacts"]
        if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 32:
            raise ValueError("attribution artifacts missing or excessive")
        paths = [_verify_artifact(record["policy_artifact"], artifact_root)]
        paths.extend(_verify_artifact(item, artifact_root) for item in artifacts)
        evidence = tuple(str(path.relative_to(artifact_root)) for path in paths)
        if record["remaining_bytes"] > record["budget_bytes"]:
            return CriterionResult(
                criterion,
                "FAIL",
                "attributed population exceeds its stated budget",
                evidence,
            )
        if record["status"] != "resolved":
            raise ValueError("attribution remains unresolved")
        return CriterionResult(
            criterion,
            "PASS",
            "attribution complete; does not waive numerical limits or policy checks",
            evidence,
        )
    except (OSError, ValueError, TypeError, KeyError) as error:
        return CriterionResult(criterion, "INCONCLUSIVE", str(error), evidence)
