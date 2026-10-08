"""Explicit limits for local packaged-artifact qualification."""

from pydantic import BaseModel, ConfigDict, Field


class ContractConfig(BaseModel):
    """Reject invalid budgets before acquiring platform resources."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    readiness_seconds: float = Field(default=60, gt=0)
    request_seconds: float = Field(default=30, gt=0)
    callback_seconds: float = Field(default=15, gt=0)
    quiet_seconds: float = Field(default=2, gt=0)
    message_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    log_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    capture_records: int = Field(default=1000, gt=0)
    capture_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    build_seconds: float = Field(default=2700, gt=0)
    builder_memory_bytes: int = Field(default=16 * 1024**3, gt=0)
    builder_cpu_quota: int = Field(default=400000, gt=0)
