"""Soak-specific input, outcome and offline command guards."""

import copy
import hashlib
import json
from pathlib import Path
from typing import cast
from uuid import uuid4

import typer
import yaml

from nanolab.tasks.soak.artifacts import fingerprint
from nanolab.tasks.soak.evaluate import combine_results, evaluate_run
from nanolab.tasks.soak.models import Status
from nanolab.tasks.soak.report import write_report

_EXIT_CODES = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "ABORTED": 130}
_METADATA = {
    "PASS": "passed",
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


def unique_soak_run_dir(runs_dir: Path) -> Path:
    """Return a fresh run directory that no earlier run can collide with."""
    return runs_dir / ("soak-" + uuid4().hex)


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


def load_soak_policy(
    data: dict[str, object], scenario_path: Path
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Resolve an external criteria policy and a receipt identifying it.

    The document carries criteria only: it cannot change the runtime, images
    or timing the run measures.
    """
    resolved = copy.deepcopy(data)
    if "soakPolicyFile" not in resolved:
        return resolved, None
    if resolved.get("workflow") != "soak":
        raise ValueError("soakPolicyFile is only supported for workflow soak")
    reference = resolved.pop("soakPolicyFile")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("soak policy file must be an explicit path")
    path = Path(reference)
    if not path.is_absolute():
        path = scenario_path.parent / path
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
    except OSError as error:
        raise ValueError(
            f"required soak policy file unavailable: {path}: {error}"
        ) from error
    if len(raw) > 1024 * 1024:
        raise ValueError("soak policy file exceeds size limit")
    policy = yaml.safe_load(raw)
    if (
        not isinstance(policy, dict)
        or set(policy) != {"schema", "criteria"}
        or policy["schema"] != "nanolab-soak-policy-v1"
        or not isinstance(policy["criteria"], list)
        or not policy["criteria"]
    ):
        raise ValueError(
            "soak policy must contain schema nanolab-soak-policy-v1 and nonempty "
            "criteria only"
        )
    soak = resolved.get("soak")
    if not isinstance(soak, dict):
        raise ValueError("soak policy requires a soak configuration block")
    soak["criteria"] = policy["criteria"]
    receipt = {
        "path": str(path.resolve()),
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "resolved_criteria_fingerprint": fingerprint({"criteria": policy["criteria"]}),
    }
    return resolved, receipt


def resolve_soak_policy(
    data: dict[str, object], scenario_path: Path
) -> dict[str, object]:
    """Return only the resolved scenario, for callers that need no receipt."""
    return load_soak_policy(data, scenario_path)[0]


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
