"""Soak-specific input, outcome and offline command guards."""

import json
from pathlib import Path
from typing import cast

import typer

from nanolab.tasks.soak.evaluate import combine_results, evaluate_run
from nanolab.tasks.soak.models import Status
from nanolab.tasks.soak.report import write_report

_EXIT_CODES = {  # nosec B105
    "PASS": 0,
    "FAIL": 1,
    "INCONCLUSIVE": 2,
    "ABORTED": 130,
}
_METADATA = {
    "PASS": "passed",  # nosec B105 - result status, not a credential
    "FAIL": "failed",
    "INCONCLUSIVE": "inconclusive",
    "ABORTED": "aborted",
}


def soak_exit_code(status: Status) -> int:
    """Map a terminal soak status onto this command's exit code."""
    return _EXIT_CODES[status]


def validate_soak_selection(
    *, resume: bool, only: str | None, start: str | None, until: str | None
) -> None:
    """Refuse a partial or resumed soak: the measurement needs its whole lifetime."""
    if resume or any(value is not None for value in (only, start, until)):
        raise ValueError(
            "soak requires its complete uninterrupted lifetime; resume/partial "
            "selection is unsupported; use soak-evaluate for saved evidence"
        )


def require_unused_run_dir(run_dir: Path) -> None:
    """Refuse a run directory that already holds evidence."""
    if run_dir.is_symlink() or (
        run_dir.exists() and (not run_dir.is_dir() or any(run_dir.iterdir()))
    ):
        raise ValueError(f"soak run directory already contains evidence: {run_dir}")


def terminal_status(run_dir: Path | None) -> Status:
    """Read the persisted terminal status; anything unreadable is INCONCLUSIVE."""
    if run_dir is None:
        return "INCONCLUSIVE"
    path = run_dir / "terminal.json"
    try:
        if path.is_symlink():
            return "INCONCLUSIVE"
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return "INCONCLUSIVE"
        terminal = json.loads(raw)
        if (
            isinstance(terminal, dict)
            and terminal.get("schema") == "nanolab-soak-v1"
            and terminal.get("status") in _EXIT_CODES
        ):
            return cast(Status, terminal["status"])
    except (OSError, ValueError, TypeError):
        pass
    return "INCONCLUSIVE"


def soak_metadata_status(run_dir: Path | None, *, aborted: bool = False) -> str:
    """Render the terminal status as the word run metadata records."""
    return _METADATA["ABORTED" if aborted else terminal_status(run_dir)]


def install_soak_commands(app: typer.Typer) -> None:
    """Register the offline soak commands on the product CLI."""

    @app.command("soak-evaluate")
    def soak_evaluate(
        run_dir: Path = typer.Argument(..., exists=True, file_okay=False),  # noqa: B008
        attribution: Path | None = typer.Option(  # noqa: B008
            None, "--attribution", exists=True, dir_okay=False
        ),
    ) -> None:
        results = evaluate_run(run_dir, attribution)
        aborted = terminal_status(run_dir) == "ABORTED"
        path = write_report(run_dir, results, aborted)
        status = combine_results(results, aborted)
        typer.echo(f"{status}: {path}")
        raise typer.Exit(soak_exit_code(status))
