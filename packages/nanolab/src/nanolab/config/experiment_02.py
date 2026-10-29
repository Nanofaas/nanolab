"""The protocol of experiment 02: complete platforms under one CPU/memory budget.

Experiment 02 sends experiment 01's calendar and payload to complete FaaS
deployments, nanoFaaS and the baselines it is compared with, each confined to a
cgroup whose CPU quota and memory limit are the budget. The arrival calendar
and operational limits are shared with experiment 01.
All correct responses count as useful; latency is reported independently.
Historical objective fields remain readable without qualifying new comparisons.

What this protocol adds is the budget grid. Every configuration runs under every
budget, and the reference budget is the one the summary table reports; the
others describe service and resource use across the grid. A budget covers the
whole deployment, not one container, so it has no per-component split.

The order seed belongs to the protocol rather than to the command line: the order
the cells run in is part of what a resume must reproduce.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nanolab.config.experiment_01 import (
    ExperimentCalendar,
    PositiveInt,
    PositiveNumber,
    SustainabilityCriteria,
)

EXPERIMENT_02_FUNCTION = "qr-code-rust"


class Budget(BaseModel):
    """The CPU quota and memory limit of one complete deployment."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        populate_by_name=True,
        frozen=True,
    )

    cpus: PositiveNumber
    memory_mib: PositiveInt = Field(alias="memoryMiB")

    @property
    def label(self) -> str:
        """Return the name this budget takes in run directories and identities."""
        return f"cpu{self.cpus:g}-mem{self.memory_mib}"


class Experiment02Protocol(ExperimentCalendar):
    """A shared calendar and budget grid, with correctness-only service."""

    # Kept for loading historical scenarios; no experiment-02 service verdicts.
    criteria: SustainabilityCriteria | None = None

    budgets: list[Budget] = Field(min_length=1)
    reference_budget: Budget = Field(alias="referenceBudget")
    readiness_timeout_seconds: PositiveInt = Field(alias="readinessTimeoutSeconds")
    # The sha256 of the PNG the payload must produce. Open in a pilot, which is
    # where it is read off a correct response; required once frozen, so a
    # response with the right shape and the wrong image is never counted.
    png_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = Field(
        default=None, alias="pngSha256"
    )
    order_seed: Annotated[int, Field(ge=0)] = Field(alias="orderSeed")

    @model_validator(mode="after")
    def coherent_budgets(self) -> Self:
        """Reject a grid with repeats or a reference budget it does not contain."""
        labels = [budget.label for budget in self.budgets]
        if len(set(labels)) != len(labels):
            raise ValueError("experiment02 budgets must be unique")
        if self.reference_budget.label not in labels:
            raise ValueError("experiment02 referenceBudget must be one of the budgets")
        if self.status == "frozen" and self.png_sha256 is None:
            raise ValueError("a frozen experiment02 protocol requires pngSha256")
        return self
