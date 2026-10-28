"""Concrete campaign views, constructed only at the observation launch boundary."""

import json
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import asdict
from datetime import UTC, datetime

from sonata_engine import WorkflowEvent

from nanolab.cli.progress import ConsoleProgressSink
from nanolab.one_shot.presentation import SinkCampaignPresentation, campaign_line


class JsonCampaignPresentation:
    """Retain the established machine-readable stdout rows."""

    def emit(self, event: WorkflowEvent) -> None:
        """Serialize task events; raw command lines keep the console policy."""
        if event.kind != "log.line":
            row = asdict(event)
            row["at"] = event.at.isoformat()
            self.campaign_event({"atUtc": datetime.now(UTC).isoformat(), **row})

    def status(self, label: str) -> AbstractContextManager[None]:
        """Status rows arrive through the campaign channel."""
        return nullcontext()

    def campaign_event(self, row: dict) -> None:
        """Write one established JSON row."""
        print(json.dumps(row, default=str), flush=True)


class ConsoleCampaignPresentation(SinkCampaignPresentation):
    """Reuse the ordinary task console and add readable campaign rows."""

    def __init__(self, *, write=None, **kwargs):
        """Accept the same output and clock options as ConsoleProgressSink."""
        self.write = write or (lambda line: print(line, flush=True))
        super().__init__(ConsoleProgressSink(write=self.write, **kwargs))

    def campaign_event(self, row: dict) -> None:
        """Write one concise operational line."""
        self.write(campaign_line(row))


class TuiCampaignPresentation(SinkCampaignPresentation):
    """Reuse the workflow dashboard's hierarchy and log pane."""

    def __init__(self, dashboard, sink):
        """Keep dashboard ownership in its existing controller."""
        super().__init__(sink)
        self.dashboard = dashboard

    def campaign_event(self, row: dict) -> None:
        """Show the actual schedule state, without adding synthetic tasks."""
        if row.get("cause") or row.get("error"):
            self.dashboard.error_detail = str(row.get("cause") or row["error"])
        observation = row.get("observation")
        if isinstance(observation, dict):
            self.dashboard.summary_lines = [
                " ".join(
                    f"{key}={observation[key]}"
                    for key in ("mode", "phase", "epoch")
                    if observation.get(key) is not None
                )
            ]
        super().campaign_event(row)


@contextmanager
def open_presentation(mode: str, *, on_failure=None, console=None):
    """Open only the selected view; the supervisor owns this context's lifetime."""
    if mode == "none":
        yield None
    elif mode == "json":
        yield JsonCampaignPresentation()
    elif mode == "console":
        yield ConsoleCampaignPresentation()
    elif mode == "tui":
        from nanolab.tui.workflow_controller import TuiWorkflowController

        controller = TuiWorkflowController(
            **({"console": console} if console is not None else {})
        )
        with controller.live_display(
            title="One-shot observation",
            summary_lines=[],
            wait_for_acknowledgment=False,
            on_failure=on_failure,
        ) as (dashboard, sink):
            yield TuiCampaignPresentation(dashboard, sink)
    else:
        raise ValueError("unknown campaign display: " + mode)


def select_presentation(mode: str, *, on_failure=None):
    """Construct a sink or a live context inside supervised execution."""
    if mode == "none":
        return None
    if mode == "console":
        return ConsoleCampaignPresentation()
    if mode == "json":
        return JsonCampaignPresentation()
    if mode == "tui":
        return open_presentation(mode, on_failure=on_failure)
    raise ValueError("unknown campaign display: " + mode)


def show_launch_failure(mode: str, error: BaseException) -> None:
    """Format failures before supervision; never fall back from a broken view."""
    try:
        with open_presentation(mode) as view:
            if view is not None:
                view.campaign_event(
                    {"cause": str(error), "exitCode": getattr(error, "exit_code", 1)}
                )
    except Exception as rendering:
        error.add_note("presentation: " + str(rendering))
