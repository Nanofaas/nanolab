"""Host-only Sonata build transport using the shared owned-process runner."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

from sonata_tasks.errors import UnsupportedCommandOptionError
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from nanolab.tasks.soak.processes import OwnedCommandResult, run_owned_command


class BuildCommandError(RuntimeError):
    """The command could not produce complete, bounded build evidence."""


def _positive(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


def _read_only(argv: tuple[str, ...]) -> bool:
    """Only provenance observations with no build/pull/bootstrap operation."""
    if argv in (("docker", "version"), ("docker", "buildx", "version")):
        return True
    if len(argv) == 4 and argv[:3] == ("docker", "buildx", "inspect"):
        return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", argv[3]) is not None
    return (
        len(argv) == 7
        and argv[:4] == ("docker", "buildx", "imagetools", "inspect")
        and re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", argv[4]) is not None
        and not argv[4].startswith("-")
        and argv[5] == "--format"
        and argv[6]
        in (
            "{{json .Manifest}}",
            "{{json .Image}}",
            "{{json .Provenance}}",
        )
    )


class OwnedBuildCommandExecutor:
    """Structural implementation of Sonata's ``CommandTaskExecutor`` protocol.

    Construction/binding/dry-run perform no filesystem or process I/O. Calls
    serialize within this instance and share a log budget, including files
    already present in ``log_dir``. Supply a dedicated directory and reserve
    any upstream receipt/metadata budget separately. Build stdout is retained
    only in ``last_log_path``, never copied into ``TaskResult``.
    """

    def __init__(
        self,
        *,
        cwd: Path,
        log_dir: Path,
        cancelled: Event,
        timeout_cap_s: float,
        artifact_limit_bytes: int,
        command_output_limit_bytes: int = 8 * 1024 * 1024,
        observation_limit_bytes: int = 1024 * 1024,
        env: Mapping[str, str] | None = None,
        target_key: str = "local",
    ):
        """Freeze host execution policy without filesystem or process I/O."""
        self._cwd, self._log_dir = Path(cwd), Path(log_dir)
        if not self._cwd.is_absolute() or not self._log_dir.is_absolute():
            raise ValueError("cwd and log_dir must be explicit absolute paths")
        self._timeout_cap = _positive(timeout_cap_s, "timeout_cap_s")
        for name, value in (
            ("artifact_limit_bytes", artifact_limit_bytes),
            ("command_output_limit_bytes", command_output_limit_bytes),
            ("observation_limit_bytes", observation_limit_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(target_key, str) or not target_key.strip():
            raise ValueError("target_key must be nonempty")
        self._env = dict(env or {})
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self._env.items()
        ):
            raise ValueError("env must map strings to strings")
        self._cancelled = cancelled
        self._limit = artifact_limit_bytes
        self._command_limit = command_output_limit_bytes
        self._observation_limit = observation_limit_bytes
        self._target_key = target_key
        self._spent = 0
        self._lock = Lock()
        self.last_log_path: Path | None = None
        self.last_result: OwnedCommandResult | None = None

    def binding_key(self, role: str) -> str:
        """Identify the host binding and reject remote roles."""
        if role != "host":
            raise UnsupportedCommandOptionError(
                "owned build executor accepts only the host role"
            )
        return self._target_key

    def _execute(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        capture_limit: int,
    ) -> tuple[OwnedCommandResult, Path, tuple[str, ...]]:
        # The lock also makes quota allocation atomic between calls on this
        # executor. The caller owns the directory across executor instances.
        with self._lock:
            self.last_log_path = None
            self.last_result = None
            if self._cancelled.is_set():
                raise KeyboardInterrupt(
                    "build cancelled before launch; no owned children to reap"
                )
            existing = (
                sum(
                    path.stat().st_size
                    for path in self._log_dir.rglob("*")
                    if path.is_file()
                )
                if self._log_dir.exists()
                else 0
            )
            self._spent = max(self._spent, existing)
            allowance = min(capture_limit, self._limit - self._spent)
            if allowance <= 0:
                raise BuildCommandError("overall build log artifact budget exhausted")
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log = self._log_dir / ("command-" + uuid4().hex + ".log")
            self.last_log_path = log
            try:
                result = run_owned_command(
                    tuple(argv),
                    cwd=cwd,
                    env=env,
                    log_path=log,
                    timeout_s=min(timeout_s, self._timeout_cap),
                    cancelled=self._cancelled,
                    output_limit_bytes=allowance,
                )
                self.last_result = result
            finally:
                size = log.stat().st_size if log.exists() else 0
                self._spent += size
            if result.cancelled or self._cancelled.is_set():
                if result.reaped is not True:
                    raise BuildCommandError(
                        "build cancellation cleanup is unconfirmed; "
                        "owned children not reaped"
                    )
                raise KeyboardInterrupt(
                    f"build cancelled after owned children were reaped; log: {log}"
                )
            failures = list(result.errors)
            if result.reaped is not True:
                failures.append("owned children were not reaped")
            if result.forced_stop:
                failures.append("owned command required a forced stop")
            if result.timed_out:
                failures.append("command timeout exceeded")
            if result.quota_exceeded:
                failures.append("command output quota exceeded")
            if type(result.returncode) is not int:
                failures.append("command exit code unavailable or invalid")
            if not log.is_file():
                failures.append("command log unavailable")
            if size > allowance or self._spent > self._limit:
                failures.append("command capture exceeded the artifact bound")
            if result.log_bytes != size:
                failures.append("command log byte count differs from observed file")
            if result.summary_bytes or result.summary_complete:
                failures.append("unexpected summary output in build command transport")
            return result, log, tuple(failures)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        """Return Sonata status; cancellation propagates after owned reaping."""
        self.binding_key(task.role)
        options = task.options
        if options.remote_dir is not None:
            raise UnsupportedCommandOptionError(
                "owned build executor does not support remote_dir"
            )
        timeout = (
            self._timeout_cap
            if options.timeout_seconds is None
            else _positive(options.timeout_seconds, "timeout_seconds")
        )
        if dry_run:
            return TaskResult(
                task.task_id, "skipped", expected_exit_codes=options.expected_exit_codes
            )
        cwd = options.cwd if options.cwd is not None else self._cwd
        if not cwd.is_absolute():
            cwd = self._cwd / cwd
        env = {**os.environ, **self._env, **options.env}
        try:
            result, log, failures = self._execute(
                task.argv,
                cwd=cwd,
                env=env,
                timeout_s=timeout,
                capture_limit=self._command_limit,
            )
        except BuildCommandError as exc:
            if "cancellation cleanup" in str(exc):
                raise
            return TaskResult(
                task.task_id,
                "failed",
                expected_exit_codes=options.expected_exit_codes,
                stderr=str(exc),
            )
        accepted = not failures and result.returncode in options.expected_exit_codes
        reason = (
            "; ".join(failures)
            if failures
            else f"unexpected command exit code {result.returncode}"
        )
        # Sonata's CommandTask requires status and exit code to agree: a
        # "failed" result carrying an ACCEPTED exit code is rejected as
        # incoherent, and the run dies with an unreadable RuntimeError instead
        # of this reason. That happens whenever the leader exits 0 but the
        # supervisor still had to reap an adopted stray (forced_stop) - a
        # routine Gradle outcome. Report no exit code when the supervisor, not
        # the command, is what failed; `reason` keeps the observed value.
        code = result.returncode
        if not accepted and code in options.expected_exit_codes:
            reason = f"{reason} (observed exit code {code})"
            code = None
        return TaskResult(
            task.task_id,
            "passed" if accepted else "failed",
            code,
            options.expected_exit_codes,
            stdout="",
            stderr="" if accepted else f"{reason}; log: {log}",
        )

    def observe(self, argv: Sequence[str], timeout_s: float) -> bytes:
        """Bounded read-only Docker provenance capture for BuildProvenanceCollector.

        Allows digest-pinned Manifest/Image/Provenance inspection, named builder
        inspection without bootstrap, and Docker/buildx version queries. Other
        commands fail before directory creation or process execution.
        """
        timeout = _positive(timeout_s, "timeout_s")
        if isinstance(argv, str) or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("read-only observation requires an argv sequence")
        command = tuple(argv)
        if not _read_only(command):
            raise ValueError(
                "command is not an allowed read-only provenance observation"
            )
        result, log, failures = self._execute(
            command,
            cwd=self._cwd,
            env={**os.environ, **self._env},
            timeout_s=timeout,
            capture_limit=min(self._command_limit, self._observation_limit),
        )
        if failures or result.returncode != 0:
            detail = (
                "; ".join(failures) if failures else f"exit code {result.returncode}"
            )
            raise BuildCommandError(
                f"provenance observation failed: {detail}; log: {log}"
            )
        with log.open("rb") as stream:
            body = stream.read(self._observation_limit + 1)
        if len(body) > self._observation_limit:
            raise BuildCommandError("observation exceeds bounded read limit")
        return body
