"""A VM provider that records commands and keeps a fake remote file system."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.tasks.experiment_02.vm import Vm
from nanolab.tasks.vm.models import VmRequest


@dataclass
class Result:
    """What a remote command returned."""

    return_code: int = 0
    stdout: str = ""
    stderr: str = ""


Handler = Callable[[tuple[str, ...], dict[str, str] | None], Result | None]


@dataclass
class RecordingProvider:
    """Record every call; answer commands through `handler`, files from `files`.

    `handler` returns a `Result`, or `None` for the default: success with no
    output. `sha256sum`, `test -f` and transfers are answered from `files`, the
    fake remote file system.
    """

    handler: Handler = lambda _argv, _env: None
    files: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def exec_argv(
        self,
        request: VmRequest,
        argv: tuple[str, ...] | list[str],
        *,
        env: dict[str, str] | None = None,
        remote_dir: str | None = None,
        dry_run: bool = False,
    ) -> Result:
        command = tuple(argv)
        self.calls.append(("exec", command, env))
        if command[0] == "sha256sum":
            if any(path not in self.files for path in command[1:]):
                return Result(1, "", "No such file")
            return Result(
                0,
                "".join(
                    f"{hashlib.sha256(self.files[path]).hexdigest()}  {path}\n"
                    for path in command[1:]
                ),
            )
        if command[:2] == ("test", "-f"):
            return Result(0 if command[2] in self.files else 1)
        return self.handler(command, env) or Result()

    def transfer_to(
        self, request: VmRequest, *, source: Path, destination: str
    ) -> Result:
        self.calls.append(("put", destination))
        self.files[destination] = Path(source).read_bytes()
        return Result()

    def transfer_from(
        self, request: VmRequest, *, source: str, destination: Path
    ) -> Result:
        self.calls.append(("get", source))
        if source not in self.files:
            return Result(1, "", "No such file")
        Path(destination).write_bytes(self.files[source])
        return Result()

    def commands(self) -> list[tuple[str, ...]]:
        """Return the argv of every command run, in order."""
        return [call[1] for call in self.calls if call[0] == "exec"]  # type: ignore[misc]


def request(name: str = "stack") -> VmRequest:
    """Return a minimal VM request for the fake provider."""
    return VmRequest(lifecycle="multipass", name=name)


def vm(provider: RecordingProvider, name: str = "stack") -> Vm:
    """Return a `Vm` on the fake provider, typed as the real port."""
    return Vm(cast(VmCommandProvider, provider), request(name))
