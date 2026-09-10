"""Best-effort Telegram notifications for completed workflows."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Mapping
from typing import final, override
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sonata_engine import WorkflowCompletion, WorkflowObserver

MessageSender = Callable[[str], None]


@final
class TelegramWorkflowObserver(WorkflowObserver):
    """Send one chat message when a workflow finishes, ignoring send failures."""

    def __init__(self, *, scenario: str, send: MessageSender) -> None:
        """Store the scenario name and the sender each message is handed to."""
        self._scenario: str = scenario
        self._send: MessageSender = send

    @override
    def finished(self, completion: WorkflowCompletion) -> None:
        with contextlib.suppress(Exception):
            self._send(_message(completion, self._scenario))


def telegram_observer_from_environment(
    scenario: str,
    environ: Mapping[str, str] | None = None,
    *,
    send: MessageSender | None = None,
) -> TelegramWorkflowObserver | None:
    """Create an observer from the `NANOLAB_TELEGRAM_*` variables, or None.

    Returns None when `CI` is set or either the bot token or chat id is missing,
    so a run without Telegram configured simply has no observer.
    """
    values = os.environ if environ is None else environ
    token = values.get("NANOLAB_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = values.get("NANOLAB_TELEGRAM_CHAT_ID", "").strip()
    if values.get("CI") or not token or not chat_id:
        return None
    return TelegramWorkflowObserver(
        scenario=scenario,
        send=send or _telegram_sender(token, chat_id),
    )


def _message(completion: WorkflowCompletion, scenario: str) -> str:
    duration_seconds = max(
        0, int((completion.finished_at - completion.started_at).total_seconds())
    )
    minutes, seconds = divmod(duration_seconds, 60)
    duration = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    lines = [
        "✅ NanoLab workflow succeeded"
        if completion.error is None
        else "❌ NanoLab workflow failed",
        f"Workflow: {completion.workflow_id}",
        f"Scenario: {scenario}",
        f"Duration: {duration}",
    ]
    if completion.error is not None:
        lines.append(f"Error: {type(completion.error).__name__}: {completion.error}")
    return "\n".join(lines)


def _telegram_sender(token: str, chat_id: str) -> MessageSender:
    def send(message: str) -> None:
        payload = urlencode({"chat_id": chat_id, "text": message}).encode()
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        # `request` was built three lines up from the literal, hardcoded
        # https://api.telegram.org endpoint above, so no scheme is caller-chosen.
        with urlopen(request, timeout=10) as response:  # nosec B310
            response.read()

    return send
