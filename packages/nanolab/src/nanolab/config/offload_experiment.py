"""Explicit single-function B4 arm; workload rates are frozen after the pilot."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OffloadExperimentConfig(BaseModel):
    """Keep treatment, offered work and generator capacity in the manifest."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: Literal["disabled", "pressure", "cloud"]
    rate: int = Field(gt=0, le=10000)
    duration_seconds: int = Field(default=180, alias="durationSeconds", ge=30, le=3600)
    warmup_seconds: int = Field(default=60, alias="warmupSeconds", ge=30, le=600)
    vus: int = Field(default=512, ge=1, le=10000)
