"""Typed model of the workbench collection's host vars (its docs/vars.md)."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Component = Literal[
    "prometheus",
    "loki",
    "log-collector",
    "loadgen",
    "node-exporter",
    "cadvisor",
    "supervisor",
]

_CPU_LIST = re.compile(r"\d+(-\d+)?(,\d+(-\d+)?)*")


class Instance(BaseModel):
    """One component instance on one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9-]+$")
    component: Component
    form: Literal["container", "pod"] = "container"
    cpuset: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("cpuset")
    @classmethod
    def _kernel_cpu_list(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _CPU_LIST.fullmatch(value) or any(
            int(low) > int(high)
            for low, high in (
                part.split("-") for part in value.split(",") if "-" in part
            )
        ):
            raise ValueError(f"not a kernel CPU list: {value!r}")
        return value


class HostVars(BaseModel):
    """Everything one host is asked to run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instances: tuple[Instance, ...]
    collect_dir: str | None = None

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [instance.name for instance in self.instances]
        if len(set(names)) != len(names):
            raise ValueError("instance names must be unique per host")
        return self

    @field_validator("collect_dir")
    @classmethod
    def _absolute(cls, value: str | None) -> str | None:
        if value is not None and not PurePosixPath(value).is_absolute():
            raise ValueError("collect_dir must be absolute")
        return value


def write_host_vars(path: Path, host_vars: HostVars) -> Path:
    """Write `host_vars` as JSON with sorted keys; return `path`."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    document = host_vars.model_dump(mode="json", exclude_none=True)
    path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n")
    return path
