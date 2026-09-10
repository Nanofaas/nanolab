"""Steps a scenario bootstrap asks a host or VM to perform."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


def _empty_env() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ScenarioOperation:
    """A bootstrap step identified by a machine id and a human summary."""

    operation_id: str
    summary: str


@dataclass(frozen=True, slots=True)
class RemoteCommandOperation(ScenarioOperation):
    """A bootstrap step that runs `argv` on the VM or on the host."""

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=_empty_env)
    # "vm" means the command must run inside the VM (e.g. docker, helm, kubectl);
    # "host" means it runs on the local machine (e.g. ansible-playbook, multipass).
    execution_target: str = "host"
