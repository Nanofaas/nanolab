"""Bounded sparse-request protocol for the paired B6 workflow."""

from pydantic import BaseModel, ConfigDict, Field


class IdleReactivationConfig(BaseModel):
    """Control repetitions, idle exposure and balanced treatment ordering."""

    model_config = ConfigDict(extra="forbid")

    cycles: int = Field(default=3, ge=1, le=100)
    idle_seconds: int = Field(default=180, alias="idleSeconds", ge=120, le=3600)
    zero_first: bool = Field(default=False, alias="zeroFirst")

    @property
    def treatments(self) -> tuple[str, str]:
        """Keep the two treatments together while allowing balanced order."""
        pair = ("keep-ready", "scale-to-zero")
        return (pair[1], pair[0]) if self.zero_first else pair
