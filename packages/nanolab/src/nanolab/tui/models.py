"""Snapshot models shared by the TUI event aggregator and dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field

from sonata_engine.workflow.models import WorkflowState


@dataclass(slots=True)
class TuiPhaseSnapshot:
    """Mutable view of one phase of a workflow.

    A phase is a node in the tree the dashboard renders: a top-level planned
    step, or a nested task pushed under one by the engine's task events.
    ``task_id`` is set once the engine has assigned the phase a task;
    ``children`` holds the nested phases, each with ``parent_task_id`` pointing
    back at this phase's ``task_id``.
    """

    label: str
    task_id: str | None = None
    parent_task_id: str | None = None
    status: WorkflowState = "pending"
    detail: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    children: list[TuiPhaseSnapshot] = field(default_factory=list)


@dataclass(slots=True)
class TuiWorkflowSnapshot:
    """Detached copy of an aggregator's state, safe for a renderer to keep.

    The phases are deep copies, so mutating them does not disturb the
    aggregator; ``logs`` holds the most recent lines up to the aggregator's
    ``log_limit``, oldest first.
    """

    phases: list[TuiPhaseSnapshot]
    logs: list[str]
    show_logs: bool


__all__ = ["TuiPhaseSnapshot", "TuiWorkflowSnapshot"]
