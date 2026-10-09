"""Strict configuration of the independent local one-shot workflows."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class ArtifactReference(BaseModel):
    """An immutable prerequisite whose bytes must match the recorded hash."""

    model_config = ConfigDict(extra="forbid")
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def read_verified(self) -> bytes:
        """Reject altered prerequisites before contacting any provider."""
        content = self.path.read_bytes()
        if hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError(f"artifact hash mismatch: {self.path}")
        return content


class OneShotNode(BaseModel):
    """A separate Multipass VM for one logical edge or terminal cloud."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    kind: Literal["edge", "cloud"]
    cpus: int = Field(default=2, gt=0)
    memory_mib: int = Field(default=4096, alias="memoryMiB", gt=0)
    disk_gib: int = Field(default=20, alias="diskGiB", gt=0)
    memory_capacity_mib: int = Field(default=512, alias="memoryCapacityMiB", gt=0)


class OneShotFunction(BaseModel):
    """Declared input, resource limits and model coefficients of a function."""

    model_config = ConfigDict(extra="forbid")
    input: dict[str, JsonValue]
    cpu: float = Field(default=1, gt=0)
    memory_mib: int = Field(default=128, alias="memoryMiB", gt=0)
    max_replicas: int = Field(default=2, alias="maxReplicas", gt=0)
    utilization: float = Field(default=0.8, gt=0, le=1)
    alpha: float = Field(default=1, ge=0)
    delta: float = Field(default=0.9, ge=0)
    gamma: float = Field(default=0.1, ge=0)


class OneShotConfig(BaseModel):
    """Provider/purpose boundaries and prerequisites checked before provisioning."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    provider: Literal["multipass"]
    purpose: Literal["workflow-validation"]
    nodes: list[OneShotNode] = Field(min_length=3)
    functions: dict[str, OneShotFunction] = Field(min_length=1)
    profile: ArtifactReference | None = None
    qualification: ArtifactReference | None = None
    flow_quantum: float = Field(default=1, alias="flowQuantum", gt=0)
    seed: int = 7

    @model_validator(mode="after")
    def validate_topology(self) -> Self:
        """Prevent aliases of a node and require a distinct terminal cloud."""
        if len({node.id for node in self.nodes}) != len(self.nodes):
            raise ValueError("node identifiers must be unique")
        if sum(node.kind == "cloud" for node in self.nodes) != 1:
            raise ValueError("one terminal cloud is required")
        if sum(node.kind == "edge" for node in self.nodes) < 2:
            raise ValueError("at least two independent edges are required")
        if any(node.memory_capacity_mib >= node.memory_mib for node in self.nodes):
            raise ValueError("replica memory must leave room for the node runtime")
        return self
