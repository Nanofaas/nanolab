"""Drive a workflow on a Rich ``Live`` dashboard, from start to final frame."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from contextlib import contextmanager
from threading import RLock
from typing import Any

from rich.console import Console
from rich.live import Live
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_engine.workflow.event_builders import build_task_event
from tui_toolkit.console import console as default_console

from nanolab.tui.event_aggregator import WorkflowEventAggregator
from nanolab.tui.models import TuiPhaseSnapshot, TuiWorkflowSnapshot
from nanolab.tui.workflow import TuiWorkflowSink, WorkflowDashboard, WorkflowKeyListener


def _flatten_phase_snapshots(snapshot: TuiWorkflowSnapshot) -> list[TuiPhaseSnapshot]:
    phases: list[TuiPhaseSnapshot] = []

    def collect(items: list[TuiPhaseSnapshot]) -> None:
        for item in items:
            phases.append(item)
            collect(item.children)

    collect(snapshot.phases)
    return phases


def _failure_targets(phases: list[TuiPhaseSnapshot]) -> list[TuiPhaseSnapshot]:
    targets = [phase for phase in phases if phase.status == "running"]
    if not targets:
        pending = next(
            (phase for phase in phases if phase.status == "pending"),
            None,
        )
        targets = [pending] if pending is not None else []
    if not targets:
        targets = [
            TuiPhaseSnapshot(label="Workflow failed", task_id="workflow.failure")
        ]
    return targets


def _handle_action_failure(
    dashboard: WorkflowDashboard,
    sink: TuiWorkflowSink,
    aggregator: WorkflowEventAggregator,
    exc: BaseException,
) -> BaseException:
    dashboard.error_detail = traceback.format_exc(limit=12)
    phases = _flatten_phase_snapshots(aggregator.snapshot())
    for phase in _failure_targets(phases):
        sink.emit(
            build_task_event(
                kind="task.failed",
                task_id=phase.task_id,
                parent_task_id=phase.parent_task_id,
                title=phase.label,
                detail=str(exc),
            )
        )
    return exc


def _record_error(
    error: BaseException | None,
    exc: BaseException,
    note: str,
) -> BaseException:
    if error is None:
        return exc
    error.add_note(note)
    return error


def _stop_listener(
    listener: WorkflowKeyListener,
    error: BaseException | None,
) -> BaseException | None:
    try:
        listener.stop()
    except Exception as cleanup_error:
        error = _record_error(
            error,
            cleanup_error,
            f"Workflow listener cleanup failed: {cleanup_error}",
        )
    return error


class TuiWorkflowController:
    """Run a workflow action behind a live dashboard.

    Kept as a small object rather than a function so callers can supply the
    Rich console the display renders to.
    """

    def __init__(self, *, console: Console = default_console) -> None:
        """Use ``console`` for the live display and screen clearing."""
        self.console = console

    @contextmanager
    def live_display(
        self,
        *,
        title: str,
        summary_lines: list[str],
        planned_steps: list[str] | None = None,
        wait_for_acknowledgment: bool = True,
        on_failure: Callable[[Exception], None] | None = None,
    ):
        """Own terminal setup/teardown without owning workflow execution.

        Refresh synchronously on events and keypresses: Rich's autonomous refresh
        thread cannot propagate output errors to a supervising caller. Listener
        errors use the same callback, and shutdown completes before returning.
        """
        aggregator = WorkflowEventAggregator(planned_steps=planned_steps)
        dashboard = WorkflowDashboard(
            title=title,
            breadcrumb=f"Main / {title}",
            footer_hint="l toggle logs | Ctrl+C exit",
            summary_lines=summary_lines,
            aggregator=aggregator,
        )
        lock = RLock()
        live = None
        broken = False

        def failed(exc):
            nonlocal broken
            if broken:
                return
            broken = True
            if on_failure is not None:
                on_failure(exc)

        def refresh():
            if broken:
                return
            try:
                with lock:
                    dashboard.sync_from_snapshot(aggregator.snapshot())
                    if live is not None:
                        live.update(dashboard.render(), refresh=True)
            except Exception as exc:
                failed(exc)
                raise

        sink = TuiWorkflowSink(aggregator, refresh=refresh)
        listener = WorkflowKeyListener(dashboard, refresh, on_failure=failed)
        display = Live(
            dashboard.render(),
            console=self.console,
            refresh_per_second=8,
            transient=False,
            auto_refresh=False,
        )
        error = None
        try:
            self.console.clear()
            live = display.__enter__()
            refresh()
            listener.start()
            yield dashboard, sink
            if wait_for_acknowledgment:
                dashboard.footer_hint = "Press any key to continue"
                refresh()
                if listener.input_is_tty:
                    listener.wait_for_acknowledgment()
        except BaseException as exc:
            error = exc
        finally:
            # Join without the refresh lock: the key thread may be in refresh.
            error = _stop_listener(listener, error)
            if broken:
                # Rich.stop still restores redirects, hooks and cursor, but its
                # final refresh must not invoke a failed renderer again.
                display.refresh = lambda: None
            try:
                display.__exit__(None, None, None)
            except BaseException as exc:
                error = _record_error(
                    error, exc, f"Workflow display cleanup failed: {exc}"
                )
        if error is not None:
            raise error

    def run_live_workflow(
        self,
        *,
        title: str,
        summary_lines: list[str],
        planned_steps: list[str] | None,
        action: Callable[[WorkflowDashboard, TuiWorkflowSink], Any],
        wait_for_acknowledgment: bool = True,
    ) -> Any:
        """Run the existing interactive workflow with optional acknowledgment."""
        with (
            self.live_display(
                title=title,
                summary_lines=summary_lines,
                planned_steps=planned_steps,
                wait_for_acknowledgment=wait_for_acknowledgment,
            ) as (dashboard, sink),
            bind_workflow_sink(sink),
        ):
            try:
                return action(dashboard, sink)
            except Exception as exc:
                try:
                    _handle_action_failure(
                        dashboard,
                        sink,
                        dashboard._aggregator,  # noqa: SLF001 - owned dashboard
                        exc,
                    )
                except Exception as rendering:
                    exc.add_note("Workflow failure display: " + str(rendering))
                raise


__all__ = ["TuiWorkflowController"]
