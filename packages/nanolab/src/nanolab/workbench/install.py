"""Install the pinned nanofaas.workbench collection into the workspace."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, override

import yaml
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome

from nanolab.tasks.infra.ansible import bundled_ansible_root, workbench_collections_path
from nanolab.tasks.resource_tasks import release_call

_REQUIREMENTS = "requirements.yml"


def _pinned_version() -> str:
    data: dict[str, Any] = yaml.safe_load(
        (bundled_ansible_root() / _REQUIREMENTS).read_text()
    )
    return str(data["collections"][0]["version"]).removeprefix("v")


def _installed_version(collections: Path) -> str | None:
    manifest = collections / "ansible_collections/nanofaas/workbench/MANIFEST.json"
    try:
        return json.loads(manifest.read_text())["collection_info"]["version"]
    except (OSError, KeyError, ValueError):
        return None


class InstallWorkbench(Task[Path]):
    """`ansible-galaxy collection install` the pin, unless it is already there."""

    title = "Install the workbench collection"
    idempotent = True

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[Path]:
        del inputs
        collections = workbench_collections_path()
        if _installed_version(collections) == _pinned_version():
            return TaskOutcome(value=collections)
        done = subprocess.run(
            [
                "ansible-galaxy",
                "collection",
                "install",
                "--force",
                "-r",
                str(bundled_ansible_root() / _REQUIREMENTS),
                "-p",
                str(collections),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            raise RuntimeError(
                f"installing the workbench collection failed: "
                f"{(done.stderr or done.stdout).strip()}"
            )
        return TaskOutcome(value=collections)


def workbench_resource() -> Resource[Path]:
    """Return the resource members require for the installed collections path."""
    return Resource(
        "Install the workbench collection",
        InstallWorkbench(),
        release_call("Release the workbench collection", lambda _i, _v: None),
    )
