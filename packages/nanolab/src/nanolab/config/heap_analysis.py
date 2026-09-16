"""Config contract for the heap-analysis workflow.

A bounded k6 workload around two post-full-GC control-plane heap dumps, each
run through a headless Eclipse MAT report. Only the JVM control plane is a
dump target; the functions driven by `workload` are load, not observations.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nanolab.config.soak import ImageBuildSpec, RolePolicy, WorkloadConfig


class HeapAnalysisConfig(BaseModel):
    """A complete heap-analysis protocol: build, workload and MAT budgets."""

    model_config = ConfigDict(extra="forbid")

    target: str = "control-plane"
    warmup_s: int = Field(gt=0)
    steady_s: int = Field(gt=0)
    drain_s: int = Field(gt=0)
    roles: dict[str, RolePolicy]
    images: dict[str, ImageBuildSpec]
    workload: WorkloadConfig
    max_dumps: int = 2
    max_dump_bytes: int = Field(gt=0)
    artifact_limit_bytes: int = Field(gt=0)
    diagnostic_timeout_s: int = Field(gt=0)
    mat_memory_mib: int = Field(gt=0)
    mat_cpus: float = Field(gt=0)
    mat_timeout_s: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        """Keep this a control-plane, two-dump protocol.

        The helper image is deliberately absent: it is built and digest-frozen
        per run, because a digest names bytes in one registry and travels with
        nobody.
        """
        if self.target != "control-plane":
            raise ValueError("target must be control-plane")
        if self.max_dumps != 2:
            raise ValueError("heap analysis captures exactly two dumps")
        if set(self.roles) != set(self.images):
            raise ValueError("roles and images must name the same set of keys")
        control_plane = self.roles.get("control-plane")
        if control_plane is None or control_plane.runtime != "jvm":
            raise ValueError("control-plane role must run the jvm runtime")
        if len(self.workload.rates) != 2:
            raise ValueError("heap analysis workload requires exactly two functions")
        # Without this the workload can name functions the deployment never
        # builds (or leave a declared role undriven): `inspect` and `plan`
        # accept the scenario and the run dies minutes in, mid-deployment.
        if set(self.workload.rates) | {"control-plane"} != set(self.roles):
            raise ValueError(
                "workload rates must cover exactly the non-control-plane roles"
            )
        return self
