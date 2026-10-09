"""Resolve a running NanoFaaS replica without depending on its container name."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sonata_engine import TaskInputs
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions

from nanolab.tasks.execution import ExecutionRole


def inspect_managed_container(
    *,
    function: str,
    replica: int,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    inputs: TaskInputs,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Select exactly one replica by labels, then verify its immutable identity."""
    subject = f"{function} replica {replica}"
    labels = {
        "io.nanofaas.managed": "true",
        "io.nanofaas.function": function,
        "io.nanofaas.replica": str(replica),
    }
    result = (
        CommandTask(
            title=f"Locate managed container for {subject}",
            argv=(
                "docker",
                "ps",
                "--no-trunc",
                "-q",
                *(
                    part
                    for key, value in labels.items()
                    for part in ("--filter", f"label={key}={value}")
                ),
            ),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
        )
        .run(inputs)
        .value
    )
    identifiers = result.stdout.split() if result else []
    if len(identifiers) != 1:
        raise RuntimeError(
            f"{subject}: expected exactly one running managed container, "
            f"got {len(identifiers)}"
        )
    identifier = identifiers[0]
    if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
        raise RuntimeError(f"{subject}: invalid container identity {identifier!r}")
    result = (
        CommandTask(
            title=f"Inspect managed container for {subject}",
            argv=("docker", "inspect", "--format={{json .}}", identifier),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
        )
        .run(inputs)
        .value
    )
    stdout = result.stdout if result else ""
    try:
        observed = json.loads(stdout)
    except ValueError as error:
        raise RuntimeError(f"{subject} was not JSON: {stdout[:200]!r}") from error
    if not isinstance(observed, dict):
        raise RuntimeError(f"{subject}: invalid container identity: expected an object")
    config = observed.get("Config")
    state = observed.get("State")
    actual_labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        observed.get("Id") != identifier
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or not isinstance(actual_labels, dict)
        or any(actual_labels.get(key) != value for key, value in labels.items())
    ):
        raise RuntimeError(f"{subject}: inspected container identity does not match")
    return observed
