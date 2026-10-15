"""Campaign views over Sonata's sink protocol; concrete UI imports stay lazy."""

from contextlib import AbstractContextManager
from typing import Protocol

from sonata_engine import WorkflowEvent
from sonata_engine.workflow.event_builders import build_log_event
from sonata_engine.workflow.events import WorkflowSink


class CampaignPresentation(Protocol):
    """A task sink with a separate channel for structured campaign progress."""

    def emit(self, event: WorkflowEvent) -> None:
        """Render the original task event."""
        ...

    def status(self, label: str) -> AbstractContextManager[None]:
        """Render a Sonata status block."""
        ...

    def campaign_event(self, row: dict) -> None:
        """Render a detached campaign row, without writing evidence."""
        ...


class SinkCampaignPresentation:
    """Adapt a caller-owned standard sink without changing its lifetime."""

    def __init__(self, sink: WorkflowSink):
        """Keep the original sink and event objects."""
        self.sink = sink

    def emit(self, event: WorkflowEvent) -> None:
        """Forward a task event once."""
        self.sink.emit(event)

    def status(self, label: str) -> AbstractContextManager[None]:
        """Use the caller's status display."""
        return self.sink.status(label)

    def campaign_event(self, row: dict) -> None:
        """Put operational progress in the existing log pane."""
        self.sink.emit(build_log_event(line=campaign_line(row)))


def campaign_line(row: dict) -> str:
    """Format optional progress fields without inventing execution tasks."""
    label = row.get("kind", row.get("stage", row.get("state", "campaign")))
    details = " ".join(
        f"{key}={value}"
        for key, value in row.items()
        if key not in {"kind", "stage", "atUtc"} and value is not None
    )
    return f"[{label}] {details}".rstrip()


class JsonCampaignPresentation:
    """Retain the established machine-readable stdout rows."""

    def emit(self, event: WorkflowEvent) -> None:
        """Serialize task events; raw command lines keep the console policy."""
        from dataclasses import asdict
        from datetime import UTC, datetime

        if event.kind != "log.line":
            row = asdict(event)
            row["at"] = event.at.isoformat()
            self.campaign_event({"atUtc": datetime.now(UTC).isoformat(), **row})

    def status(self, label: str) -> AbstractContextManager[None]:
        """Status rows arrive through the campaign channel."""
        from contextlib import nullcontext

        return nullcontext()

    def campaign_event(self, row: dict) -> None:
        """Write one established JSON row."""
        import json

        print(json.dumps(row, default=str), flush=True)
