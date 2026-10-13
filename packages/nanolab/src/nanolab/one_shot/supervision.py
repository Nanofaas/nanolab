"""Own campaign health, Sonata progress, interruption and serial archiving."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any

from sonata_engine import Resource, WorkflowEvent, bind_workflow_sink


class CampaignInterruptedError(RuntimeError):
    """An operator signal whose exit code is retained through compensation."""

    def __init__(self, signum: int):
        """Retain the conventional shell exit code."""
        super().__init__(f"campaign interrupted by signal {signum}")
        self.exit_code = 128 + signum


class CampaignSupervision:
    """Supervise acquired resources without changing Sonata dependency semantics."""

    def __init__(self, output: Path, *, archive: Path | None = None, cadence=5.0):
        """Use cadence for health polling/reporting, never a performance gate."""
        self.output, self.archive, self.cadence = output, archive, cadence
        self.lock = RLock()
        self.stop = Event()
        self.thread = Thread(target=self._monitor, name="one-shot-supervision")
        self.checks: dict[str, Callable[[], None]] = {}
        self.writers: list[Any] = []
        self.active: dict[str, str] = {}
        self.aborting = False
        self.fault: BaseException | None = None
        self.fault_dispatched = False
        self.finishing = False
        self.primary: BaseException | None = None
        self.interruption: CampaignInterruptedError | None = None
        self.state: dict[str, Any] = {
            "state": "starting",
            "activity": "progress",
            "phase": "initialization",
            "exitCode": None,
            "cause": None,
            "anomalies": [],
            "archive": str(archive) if archive else None,
            "archiveVerified": False,
        }

    def _write_status(self):
        self.state["atUtc"] = datetime.now(UTC).isoformat()
        temporary = self.output / "status.json.tmp"
        temporary.write_text(json.dumps(self.state, indent=2) + "\n")
        temporary.replace(self.output / "status.json")

    def record(self, kind: str, **values):
        """Serialize events, state and stdout from both engine and health thread."""
        with self.lock:
            row = {"atUtc": datetime.now(UTC).isoformat(), "kind": kind, **values}
            errors = []
            try:
                with (self.output / "supervision.jsonl").open("a") as stream:
                    stream.write(json.dumps(row, default=str) + "\n")
            except OSError as error:
                errors.append(error)
            try:
                self._write_status()
            except OSError as error:
                errors.append(error)
            try:
                if kind != "log.line":
                    print(json.dumps(row, default=str), flush=True)
            except OSError as error:
                errors.append(error)
            if errors:
                self.fault = self.fault or self.primary or errors[0]
                # Reporting failures must not prevent compensation. The monitor
                # delivers the first fault independently of the broken channel.
                if not self.aborting and not self.finishing:
                    raise self.fault

    def emit(self, event: WorkflowEvent):
        """Receive every Sonata acquisition, task, nested step and compensation."""
        with self.lock:
            key = event.task_run_id or event.task_id or event.title
            if event.kind == "task.started":
                self.active[key] = event.title
                self.state.update(activity="progress", phase=event.title)
            elif event.kind in {"task.passed", "task.skipped", "task.failed"}:
                self.active.pop(key, None)
                if event.kind == "task.failed":
                    self.aborting = True
                    self.state["anomalies"].append(
                        {"title": event.title, "detail": event.detail}
                    )
                    self.state.update(state="cleanup", activity="progress")
            row = asdict(event)
            row.pop("kind")
            row["at"] = event.at.isoformat()
            try:
                self.record(event.kind, **row)
            except OSError as error:
                self.fault = self.fault or error

    @contextmanager
    def status(self, label: str) -> Generator[None, None, None]:
        """Support Sonata status blocks without introducing another renderer."""
        self.record("status.started", title=label)
        try:
            yield
        finally:
            self.record("status.finished", title=label)

    def watch(self, name: str, check: Callable[[], None], *, writer=None):
        """Register only an acquired resource; checks must be nonblocking."""
        with self.lock:
            self.checks[name] = check
            if writer is not None:
                self.writers.append(writer)
        self.record("process.watched", name=name)

    def unwatch(self, name: str):
        """Remove health checks before an intentional stop or release."""
        with self.lock:
            self.checks.pop(name, None)
        self.record("process.released", name=name)

    def resource(self, resource: Resource, name: str, check: Callable) -> Resource:
        """Wrap ownership callbacks while preserving all declared dependencies."""

        def acquire(inputs):
            value = resource.acquire(inputs)
            try:
                self.watch(name, lambda: check(value), writer=value)
            except BaseException as error:
                try:
                    resource.release(inputs, value)
                except Exception as cleanup:
                    error.add_note("registration cleanup: " + str(cleanup))
                raise
            return value

        def release(inputs, value):
            try:
                self.unwatch(name)
            finally:
                if resource.release is not None:
                    resource.release(inputs, value)
            return

        return replace(resource, acquire=acquire, release=release)

    @staticmethod
    def check_process(
        process: subprocess.Popen,
        *,
        expected_end: float,
        completed: Callable[[], bool] = lambda: False,
    ):
        """Accept a successful exit only after the frozen load has ended."""
        code = process.poll()
        if code is not None and (
            code != 0 or (time.monotonic() < expected_end and not completed())
        ):
            raise RuntimeError(f"generator stopped unexpectedly: exit {code}")

    def service_resource(
        self,
        resource: Resource,
        name: str,
        probe: Callable,
        *,
        interval_seconds: float = 5,
        join_seconds: float = 300,
    ) -> Resource:
        """Run blocking service probes outside the lock with the existing worker."""
        from nanolab.tasks.one_shot.clock import ClockMonitor

        worker = None

        def acquire(inputs):
            nonlocal worker
            value = resource.acquire(inputs)
            worker = ClockMonitor(
                lambda: probe(value),
                interval_seconds=interval_seconds,
                join_seconds=join_seconds,
            )
            try:
                worker.start()
                self.watch(name, lambda: check_background_worker(worker), writer=worker)
            except BaseException:
                try:
                    worker.close()
                finally:
                    if resource.release is not None:
                        resource.release(inputs, value)
                raise
            return value

        def release(inputs, value):
            try:
                try:
                    self.unwatch(name)
                finally:
                    if worker is not None:
                        worker.close()
            finally:
                if resource.release is not None:
                    resource.release(inputs, value)

        return replace(resource, acquire=acquire, release=release)

    def _monitor(self):
        started = time.monotonic()
        cpu_started = time.thread_time()
        while not self.stop.wait(self.cadence):
            try:
                # Lock covers checks too: unwatch waits for any current check,
                # then guarantees that cleanup cannot race the old callback.
                with self.lock:
                    if not self.aborting:
                        if self.fault is not None:
                            raise self.fault
                        for name, check in self.checks.items():
                            try:
                                check()
                            except Exception as error:
                                raise RuntimeError(f"{name}: {error}") from error
                    self.state["supervisorCost"] = {
                        "cpuSeconds": time.thread_time() - cpu_started,
                        "wallSeconds": time.monotonic() - started,
                    }
                    manifest_path = self.output / "manifest.json"
                    if manifest_path.exists():
                        manifest = json.loads(manifest_path.read_text())
                        grid = manifest.get("parameters", {}).get("logicalSchedule", {})
                        anchor = grid.get("anchorMonotonicSeconds")
                        if anchor is not None:
                            epoch = int(
                                (time.monotonic() - anchor)
                                // manifest["period_seconds"]
                            )
                            warmup = (
                                manifest.get("parameters", {})
                                .get("generator", {})
                                .get("warmupSeconds", 0)
                            )
                            epoch_count = grid.get("epochCount")
                            phase = (
                                "setup"
                                if time.monotonic() < anchor - warmup
                                else "warmup"
                                if epoch < 0
                                else "drain"
                                if epoch_count is not None and epoch >= epoch_count
                                else "campaign"
                            )
                            self.state["observation"] = {
                                "mode": manifest.get("mode"),
                                "phase": phase,
                                "epoch": epoch if phase == "campaign" else None,
                                "functions": list(
                                    manifest.get("parameters", {}).get("functions", {})
                                ),
                            }
                    self.state["activity"] = "waiting"
                    self.record(
                        "heartbeat",
                        activity="waiting",
                        phase=self.state["phase"],
                        processes=list(self.checks),
                        observation=self.state.get("observation"),
                    )
            except Exception as error:
                with self.lock:
                    if self.aborting:
                        continue
                    self.fault = error
                    self.aborting = True
                    self.state.update(state="cleanup", cause=str(error))
                    try:
                        self.record("operational.failure", cause=str(error))
                    except Exception as reporting_error:
                        error.add_note("failure reporting: " + str(reporting_error))
                # Wake a main thread blocked in warmup/provisioning/waits. The
                # handler raises once; Sonata owns the compensating releases.
                os.kill(os.getpid(), signal.SIGINT)

    def _interrupted(self, signum, _frame):
        with self.lock:
            if self.fault is not None:
                if not self.finishing and not self.fault_dispatched:
                    self.fault_dispatched = True
                    raise self.fault
                return
            if (
                self.primary is not None
                or self.state["anomalies"]
                or self.interruption is not None
            ):
                return
            self.interruption = CampaignInterruptedError(signum)
            if self.finishing:
                # Defer the first archive signal; keep raw/control files stable
                # during copy and preserve its code in the terminal snapshot.
                return
            self.aborting = True
            raise self.interruption

    def run(self, action: Callable[[], Any]):
        """Run exactly once, compensate, join writers, archive and retain cause."""
        previous = {
            sig: signal.signal(sig, self._interrupted)
            for sig in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            self._run(action)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _run(self, action):
        primary = None
        self.state["state"] = "running"
        try:
            self.record("campaign.started")
            self.thread.start()
            with bind_workflow_sink(self):
                action()
            self.finishing = True
        except BaseException as error:
            self.primary = error
            self.finishing = True
            primary = error
        finally:
            self.finishing = True
            self.aborting = True
            self.stop.set()
            if self.thread.ident is not None:
                self.thread.join()
        primary = self.fault or primary or self.interruption
        self.primary = primary
        self.state.update(
            state="failed" if primary else "completed",
            activity="terminal",
            cause=str(primary) if primary else None,
            exitCode=getattr(primary, "exit_code", 1) if primary else 0,
            notes=getattr(primary, "__notes__", []),
        )
        self.record("campaign.writers-joined")
        self._write_result()
        if self.archive is not None:
            try:
                self.state.update(state="archiving", activity="progress")
                self.record("archive.started")
                self._require_writers_stopped()
                shutil.copytree(self.output, self.archive)
                self._verify_archive()
                primary = primary or self.interruption
                self.primary = primary
                self.state.update(
                    archiveVerified=True,
                    state="failed" if primary else "completed",
                    activity="terminal",
                    cause=str(primary) if primary else None,
                    exitCode=getattr(primary, "exit_code", 1) if primary else 0,
                )
                self.record(
                    "archive.verified",
                    interruption=str(self.interruption) if self.interruption else None,
                )
                self._write_result()
                # Only this thread writes now. Copy final control files and
                # verify again; no background writer can alter either tree.
                for name in (
                    "status.json",
                    "result.json",
                    "supervision.jsonl",
                    "report.json",
                    "report.html",
                ):
                    if (self.output / name).exists():
                        shutil.copy2(self.output / name, self.archive / name)
                self._verify_archive()
            except Exception as error:
                primary = primary or self.interruption
                if primary is not None:
                    primary.add_note("archive: " + str(error))
                else:
                    primary = error
                self.state.update(
                    state="failed",
                    activity="terminal",
                    exitCode=getattr(primary, "exit_code", 1),
                    cause=str(primary),
                    archiveVerified=False,
                    notes=getattr(primary, "__notes__", []),
                )
                self.record("archive.failed", cause=str(error))
                self._write_result()
        if primary is None and self.interruption is not None:
            primary = self.interruption
            self.state.update(
                state="failed", cause=str(primary), exitCode=primary.exit_code
            )
            self.record("signal.deferred", cause=str(primary))
            self._write_result()
            if self.archive is not None and self.state["archiveVerified"]:
                for name in (
                    "status.json",
                    "result.json",
                    "supervision.jsonl",
                    "report.json",
                    "report.html",
                ):
                    if (self.output / name).exists():
                        shutil.copy2(self.output / name, self.archive / name)
                self._verify_archive()
        primary = self.fault or self.primary or primary or self.interruption
        try:
            print(json.dumps(self.state), flush=True)
        except OSError as error:
            primary = primary or error
        if primary is not None:
            raise primary

    def _require_writers_stopped(self):
        for writer in self.writers:
            thread = getattr(writer, "thread", None) or getattr(writer, "_thread", None)
            if thread is not None and thread.is_alive():
                raise RuntimeError("archive unsafe: background writer still running")
            processes = [writer] if isinstance(writer, subprocess.Popen) else []
            for session in getattr(writer, "sessions", []):
                processes.append(session.get("process"))
                if any(reader.is_alive() for reader in session.get("readers", [])):
                    raise RuntimeError("archive unsafe: raw drain writer still running")
            if any(
                process is not None and process.poll() is None for process in processes
            ):
                raise RuntimeError("archive unsafe: owned process still running")

    def _write_result(self):
        self.primary = self.fault or self.primary or self.interruption
        if self.primary is not None:
            self.state.update(
                state="failed",
                cause=str(self.primary),
                exitCode=getattr(self.primary, "exit_code", 1),
                notes=getattr(self.primary, "__notes__", []),
            )
        try:
            from nanolab.one_shot.report import finalize_operational_outcome

            (self.output / "result.json").write_text(
                json.dumps(self.state, indent=2) + "\n"
            )
            finalize_operational_outcome(self.output, self.state)
        except (OSError, ValueError) as error:
            if self.primary is None:
                self.primary = self.fault = error
                self.state.update(state="failed", cause=str(error), exitCode=1)
            self.primary.add_note("result persistence: " + str(error))

    def _verify_archive(self):
        assert self.archive is not None
        for path in self.output.rglob("*"):
            if path.is_file():
                with (
                    path.open("rb") as source,
                    (self.archive / path.relative_to(self.output)).open("rb") as target,
                ):
                    if (
                        hashlib.file_digest(source, "sha256").digest()
                        != hashlib.file_digest(target, "sha256").digest()
                    ):
                        raise ValueError(f"archive hash mismatch: {path.name}")


def check_sampler(session):
    """Reuse raw inventories for service liveness; sample gaps remain observations."""
    session.require_healthy()
    for item in session.sessions:
        path = Path(item["output"].name)
        if not path.exists():
            continue
        # Read only a tail; ignore an unfinished line while its writer is active.
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 131072))
            lines = stream.read().splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if "containers" not in row or (
                row.get("errors") and "expectedContainerIds" not in row
            ):
                break
            if any(not error.get("containerId") for error in row.get("errors", [])):
                break
            inventory = row.get("containerInventory")
            if inventory is None:
                if row.get("errors"):
                    break  # Old raw format cannot identify unreadable containers.
                inventory = row["containers"]
            roles = {container["role"] for container in inventory}
            missing = {"control-plane", "collector"} - roles
            if missing:
                node = item["node"].config.id
                raise RuntimeError(
                    f"{node}: required service stopped: {sorted(missing)}"
                )
            break


def check_background_worker(worker):
    """Check the existing error state and independently owned thread liveness."""
    worker.require_healthy()
    thread = getattr(worker, "thread", None) or getattr(worker, "_thread", None)
    stopping = getattr(worker, "stopping", False)
    stop = getattr(worker, "stop", None)
    if stop is not None:
        stopping = stopping or stop.is_set()
    if thread is not None and not thread.is_alive() and not stopping:
        raise RuntimeError("required background thread stopped")
