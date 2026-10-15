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
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, RLock, Thread, current_thread, main_thread
from typing import Any, cast

from sonata_engine import Resource, WorkflowEvent, WorkflowSink, bind_workflow_sink
from sonata_engine.workflow.context import active_sink

from nanolab.one_shot.presentation import SinkCampaignPresentation
from nanolab.one_shot.recording import CampaignRecorder

_INHERIT = object()


class CampaignInterruptedError(RuntimeError):
    """An operator signal whose exit code is retained through compensation."""

    def __init__(self, signum: int):
        """Retain the conventional shell exit code."""
        super().__init__(f"campaign interrupted by signal {signum}")
        self.exit_code = 128 + signum


class CampaignSupervision:
    """Supervise acquired resources without changing Sonata dependency semantics."""

    def __init__(
        self,
        output: Path,
        *,
        archive: Path | None = None,
        cadence=5.0,
        presentation=_INHERIT,
        presentation_factory=None,
    ):
        """Use cadence for health polling/reporting, never a performance gate."""
        self.output, self.archive, self.cadence = output, archive, cadence
        self.presentation_factory = presentation_factory
        self.presentation_context = None
        self.presentation = presentation
        self.presentation_lock = RLock()
        self.presentation_broken = False
        self.presentation_error = None
        self.deferred_errors = []
        self.releasing = False
        self.recorder = CampaignRecorder(output)
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
        self.recorder.write_status(self.state)

    def record(self, kind: str, *, _event=None, _prior=None, **values):
        """Persist before delivery; never run a view under the health lock."""
        with self.lock:
            row = {"atUtc": datetime.now(UTC).isoformat(), "kind": kind, **values}
            errors = []
            try:
                self.recorder.append_event(row)
            except OSError as error:
                errors.append(error)
            try:
                self._write_status()
            except OSError as error:
                errors.append(error)
        for error in errors:
            self._operational_failure(error, "recording", prior=_prior)
        if _event is None:
            self._render(lambda view: view.campaign_event(deepcopy(row)), prior=_prior)
        else:
            self._render(lambda view: view.emit(_event), prior=_prior)

    def progress(self, row: dict):
        """Record an observation stage once, then deliver its detached snapshot."""
        try:
            with self.lock:
                self.recorder.append_progress(row)
        except OSError as error:
            self._operational_failure(error, "recording")
        self._render(lambda view: view.campaign_event(deepcopy(row)))

    def _presentation_failed(self, error, *, prior=None):
        with self.lock:
            self.presentation_broken = True
            self.presentation_error = self.presentation_error or error
        self._operational_failure(error, "presentation", prior=prior)

    def _operational_failure(self, error, channel, *, prior=None):
        with self.lock:
            if prior is not None:
                self.fault = self.fault or self.primary or self.interruption or prior
            primary = self.primary or self.fault or self.interruption
            if primary is not None and primary is not error:
                primary.add_note(channel + ": " + str(error))
            # A failed task or signal is already unwinding Sonata. Never throw
            # from its diagnostic/compensation events and skip a release.
            if self.aborting or self.finishing:
                if self.finishing:
                    self.primary = primary or error
                elif primary is None:
                    self.deferred_errors.append((channel, error))
                return
            self.fault = primary or error
            self.aborting = True
            self.state.update(state="cleanup", cause=str(self.fault))
        if self.releasing:
            return
        if current_thread() is main_thread():
            self.fault_dispatched = True
            raise self.fault
        # Wake blocking workflow waits immediately, independently of cadence.
        os.kill(os.getpid(), signal.SIGINT)

    def _render(self, callback, *, prior=None):
        error = None
        with self.presentation_lock:
            if self.presentation is None or self.presentation_broken:
                return None
            try:
                return callback(self.presentation)
            except Exception as caught:
                # A view callback or signal handler may already have dispatched
                # this stop. Propagate it once on the main thread, while keeping
                # worker delivery and release/finalization deferment intact.
                if caught is self.fault or caught is self.interruption:
                    if (
                        self.fault_dispatched
                        and current_thread() is main_thread()
                        and not self.releasing
                        and not self.finishing
                    ):
                        raise
                    return None
                self.presentation_broken = True
                error = caught
        # Release the view lock before signalling: compensation may render.
        self._presentation_failed(error, prior=prior)
        return None

    def emit(self, event: WorkflowEvent):
        """Record original Sonata identities and forward exactly once."""
        with self.lock:
            key = event.task_run_id or event.task_id or event.title
            if event.parent_task_id is None and event.kind in {
                "task.started",
                "task.passed",
                "task.failed",
                "task.skipped",
            }:
                # Sonata Resource.release_title is the lifecycle vocabulary;
                # display faults during releases are deferred to the next unit.
                self.releasing = event.title.startswith("Release ")
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
            self.record(event.kind, _event=event, **row)
        except OSError as error:
            if self.fault_dispatched:
                raise
            self.fault = self.fault or error
        pending = self.fault or self.interruption
        if (
            pending is not None
            and not self.fault_dispatched
            and not self.releasing
            and event.kind == "task.started"
            and not self.finishing
        ):
            self.fault_dispatched = True
            raise pending

    @contextmanager
    def status(self, label: str) -> Generator[None, None, None]:
        """Compose status blocks without holding either lock over task work."""
        self.record("status.started", title=label)
        block = self._render(lambda view: view.status(label))
        if block is not None:
            self._render(lambda _: block.__enter__())
        body_error = None
        try:
            yield
        except BaseException as error:
            body_error = error
            if block is not None:
                try:
                    self._render(
                        lambda _, exc=error: block.__exit__(
                            type(exc), exc, exc.__traceback__
                        ),
                        prior=error,
                    )
                except Exception as rendering:
                    error.add_note("presentation: " + str(rendering))
            raise
        else:
            if block is not None:
                self._render(lambda _: block.__exit__(None, None, None))
        finally:
            self.record("status.finished", title=label, _prior=body_error)

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
            except BaseException as error:
                try:
                    worker.close()
                except BaseException as cleanup:
                    error.add_note("worker cleanup: " + str(cleanup))
                try:
                    if resource.release is not None:
                        resource.release(inputs, value)
                except BaseException as cleanup:
                    error.add_note("service cleanup: " + str(cleanup))
                raise
            return value

        def release(inputs, value):
            primary = None
            try:
                self.unwatch(name)
            except BaseException as error:
                primary = error
            try:
                if worker is not None:
                    worker.close()
            except BaseException as error:
                if primary is None:
                    primary = error
                else:
                    primary.add_note("worker cleanup: " + str(error))
            try:
                if resource.release is not None:
                    resource.release(inputs, value)
            except BaseException as error:
                if primary is None:
                    primary = error
                else:
                    primary.add_note("service cleanup: " + str(error))
            if primary is not None:
                raise primary

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
                if (
                    not self.finishing
                    and not self.fault_dispatched
                    and not self.releasing
                ):
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
            if self.releasing:
                return
            self.fault_dispatched = True
            raise self.interruption

    def run(self, action: Callable[[], Any]):
        """Run exactly once, compensate, join writers, archive and retain cause."""
        if self.presentation is _INHERIT:
            self.presentation = active_sink()
        if self.presentation is not None and not hasattr(
            self.presentation, "campaign_event"
        ):
            self.presentation = SinkCampaignPresentation(
                cast(WorkflowSink, self.presentation)
            )
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
            if self.presentation_factory is not None:
                selected = self.presentation_factory(self._presentation_failed)
                if hasattr(selected, "__enter__"):
                    self.presentation_context = selected
                    self.presentation = selected.__enter__()
                else:
                    self.presentation = selected
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
        self.primary = (
            self.fault or self.interruption or primary or self.presentation_error
        )
        if (
            primary is not None
            and self.primary is not None
            and self.primary is self.interruption
            and primary is not self.primary
        ):
            self.primary.add_note("workflow cleanup: " + str(primary))
        for channel, error in self.deferred_errors:
            if self.primary is None:
                self.primary = error
            elif self.primary is not error:
                self.primary.add_note(channel + ": " + str(error))
        self.deferred_errors.clear()
        self._terminal_outcome()
        self.record("campaign.writers-joined")
        persisted_interruption = self.interruption
        self._write_result()
        if self.interruption is not persisted_interruption:
            # A signal can arrive during report persistence even without an
            # archive. Persist this newly observed outcome before copying.
            self._terminal_outcome()
            self.record("signal.deferred", cause=str(self.primary))
            self._write_result()
        archive_owned = False
        if self.archive is not None:
            try:
                self.state.update(state="archiving", activity="progress")
                self.record("archive.started")
                self._require_writers_stopped()
                # Claim atomically before copy: a partial copy is ours, an
                # existing destination (even an empty one) is never touched.
                self.archive.mkdir(parents=True, exist_ok=False)
                archive_owned = True
                shutil.copytree(self.output, self.archive, dirs_exist_ok=True)
                self._verify_archive()
                self.state["archiveVerified"] = True
                self._terminal_outcome()
                self.record(
                    "archive.verified",
                    interruption=str(self.interruption) if self.interruption else None,
                )
                # Keep the view through archive progress, then stop all terminal
                # activity before the authoritative control copy/hash check.
                if self.presentation_context is not None:
                    self._close_presentation()
                persisted_interruption = self.interruption
                self._write_result()
                if not self.state["archiveVerified"]:
                    raise self.primary or RuntimeError(
                        "archive verification invalidated"
                    )
                self._copy_archive_controls()
                self._verify_archive()
                if self.interruption is not persisted_interruption:
                    self._terminal_outcome()
                    self.record("signal.deferred", cause=str(self.primary))
                    self._write_result()
                    self._copy_archive_controls()
                    self._verify_archive()
            except BaseException as error:
                self.primary = (
                    self.primary
                    or self.fault
                    or self.interruption
                    or self.presentation_error
                )
                if self.primary is None:
                    self.primary = error
                elif self.primary is not error:
                    self.primary.add_note("archive: " + str(error))
                self.state["archiveVerified"] = False
                self._terminal_outcome()
                try:
                    self.record("archive.failed", cause=str(error))
                except Exception as reporting:
                    self.primary.add_note(
                        "archive failure reporting: " + str(reporting)
                    )
                self._write_result()
                if archive_owned:
                    # Diagnostic invalidation only: never repair measurements
                    # or verify this failed archive again.
                    self._copy_archive_controls(invalidate=True)
                    self._write_result()
        self._terminal_outcome()
        before_close = deepcopy(self.state)
        self._close_presentation()
        if self.state != before_close:
            self._terminal_outcome()
            self._write_result()
            if archive_owned:
                self.state["archiveVerified"] = False
                self._write_result()
                self._copy_archive_controls(invalidate=True)
        if self.primary is not None:
            raise self.primary

    def _close_presentation(self):
        self._render(lambda view: view.campaign_event(deepcopy(self.state)))
        context, self.presentation_context = self.presentation_context, None
        self.presentation = None
        if context is not None:
            try:
                context.__exit__(None, None, None)
            except Exception as error:
                self._presentation_failed(error)
        self._terminal_outcome()

    def _terminal_outcome(self):
        self.primary = (
            self.primary or self.fault or self.interruption or self.presentation_error
        )
        self.state.update(
            state="failed" if self.primary else "completed",
            activity="terminal",
            cause=str(self.primary) if self.primary else None,
            exitCode=getattr(self.primary, "exit_code", 1) if self.primary else 0,
            notes=getattr(self.primary, "__notes__", []),
        )

    def _copy_archive_controls(self, *, invalidate=False):
        if self.archive is None:
            raise RuntimeError("archive destination is required")
        for name in (
            "status.json",
            "result.json",
            "supervision.jsonl",
            "report.json",
            "report.html",
        ):
            try:
                if invalidate and name in {"status.json", "result.json"}:
                    (self.archive / name).write_text(
                        json.dumps(self.state, indent=2) + "\n"
                    )
                elif (self.output / name).exists():
                    shutil.copy2(self.output / name, self.archive / name)
            except Exception as error:
                if not invalidate:
                    raise
                self.primary = self.primary or error
                self.primary.add_note(
                    "archive invalidation " + name + ": " + str(error)
                )

        if invalidate:
            from nanolab.one_shot.report import finalize_operational_outcome

            try:
                finalize_operational_outcome(self.archive, self.state)
            except (OSError, ValueError) as error:
                self.primary = self.primary or error
                self.primary.add_note("archive invalidation report: " + str(error))
                if self.primary is not error:
                    for note in getattr(error, "__notes__", ()):
                        self.primary.add_note(note)

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
        self._terminal_outcome()
        from nanolab.one_shot.report import finalize_operational_outcome

        errors = []
        # Each diagnostic channel is independent; storage can reject any of
        # them. Always retain the first cause and report limits on stdout.
        try:
            self.recorder.write_result(self.state)
        except (OSError, ValueError) as error:
            errors.append(error)
        try:
            self._write_status()
        except (OSError, ValueError) as error:
            errors.append(error)
        for error in errors:
            self.primary = self.primary or error
            self.primary.add_note("result persistence: " + str(error))
        self._terminal_outcome()
        if errors:
            # Persist failure and limitations wherever the terminal channel
            # is still writable. This does not retry campaign/archive work.
            for name in ("result.json", "status.json"):
                try:
                    if name == "result.json":
                        self.recorder.write_result(self.state)
                    else:
                        self.recorder.write_status(self.state)
                except OSError as error:
                    self.primary = self.primary or error
                    self.primary.add_note(
                        "result persistence " + name + ": " + str(error)
                    )
            self._terminal_outcome()

        # Reports receive the outcome after terminal receipt errors have
        # been promoted, even when this run has no archive destination.
        try:
            finalize_operational_outcome(self.output, self.state)
        except (OSError, ValueError) as error:
            self.primary = self.primary or self.interruption or error
            self.primary.add_note("result persistence: " + str(error))
            if self.primary is not error:
                for note in getattr(error, "__notes__", ()):
                    self.primary.add_note(note)
            self.state["archiveVerified"] = False
            self._terminal_outcome()
            # One diagnostic pass promotes the earlier report snapshot to the
            # failed outcome. Each writable channel is attempted independently.
            try:
                finalize_operational_outcome(self.output, self.state)
            except (OSError, ValueError) as reporting:
                self.primary.add_note("report finalization: " + str(reporting))
                if self.primary is not reporting:
                    for note in getattr(reporting, "__notes__", ()):
                        self.primary.add_note(note)
            self._terminal_outcome()
            for name in ("result.json", "status.json"):
                try:
                    if name == "result.json":
                        self.recorder.write_result(self.state)
                    else:
                        self.recorder.write_status(self.state)
                except OSError as reporting:
                    self.primary.add_note(
                        "result persistence " + name + ": " + str(reporting)
                    )
            self._terminal_outcome()

    def _verify_archive(self):
        if self.archive is None:
            raise RuntimeError("archive destination is required")
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
