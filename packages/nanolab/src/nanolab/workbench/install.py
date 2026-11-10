"""Install the pinned nanofaas.workbench collection into the workspace."""

from __future__ import annotations

import errno
import json
import re
import socket
import subprocess
from collections.abc import Iterable
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


def ensure_installed() -> Path:
    """Install the pinned collection unless it is already there; return its path."""
    collections = workbench_collections_path()
    if _installed_version(collections) == _pinned_version():
        return collections
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
    return collections


ANSIBLE_CORE = "2.20"


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    """Run a check; a missing or hung program fails it like a non-zero exit."""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(argv, 127, "", str(error))


def check_operator(ports: Iterable[tuple[str, int]]) -> None:
    """Refuse a run the operator host cannot hold, before anything deploys.

    ansible-core 2.20 on PATH whose Python imports `requests` (the
    community.docker modules need it), a reachable Docker, and every
    operator-side port (`(address, port)`) still free.
    """
    version = _run("ansible", "--version").stdout
    core = re.search(r"core (\d+\.\d+)\.", version)
    python = re.search(r"python version = .*\((/[^)]+)\)\s*$", version, re.M)
    if core is None or core[1] != ANSIBLE_CORE or python is None:
        raise RuntimeError(
            f"the workbench needs ansible-core {ANSIBLE_CORE}.x on PATH; "
            f"`ansible --version` says: {version.strip()[:200] or 'nothing'}"
        )
    if _run(python[1], "-c", "import requests").returncode:
        raise RuntimeError(
            f"ansible's Python ({python[1]}) cannot import requests, which "
            "community.docker needs: install it there"
        )
    if (docker := _run("docker", "info")).returncode:
        raise RuntimeError(f"docker info failed: {docker.stderr.strip()[:200]}")
    for address, port in ports:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((address, port))
            except OSError as error:
                # Only "in use": an address not up yet is the deploy's to report.
                if error.errno != errno.EADDRINUSE:
                    continue
                raise RuntimeError(
                    f"{address}:{port} is already bound on the operator "
                    f"(an earlier run still up?): {error}"
                ) from error


class InstallWorkbench(Task[Path]):
    """`ansible-galaxy collection install` the pin, unless it is already there."""

    title = "Install the workbench collection"
    idempotent = True

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[Path]:
        del inputs
        return TaskOutcome(value=ensure_installed())


def workbench_resource() -> Resource[Path]:
    """Return the resource members require for the installed collections path."""
    return Resource(
        "Install the workbench collection",
        InstallWorkbench(),
        release_call("Release the workbench collection", lambda _i, _v: None),
    )
