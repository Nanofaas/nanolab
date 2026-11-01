"""Frozen matched module-cost block; no load/budget/heap parameter sweep."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nanolab.config.environment import EnvironmentConfig

ARMS = tuple(
    f"{runtime}-{composition}"
    for runtime in ("jvm", "native")
    for composition in ("base", "c", "s", "cs")
)
BASE = ("k8s-deployment-provider", "async-queue", "build-metadata")
BUDGETS = {
    "control-plane": {
        "requests": {"cpu": 0.25, "memoryMiB": 512},
        "limits": {"cpu": 2, "memoryMiB": 2048},
    },
    "word-stats-java": {
        "requests": {"cpu": 1, "memoryMiB": 256},
        "limits": {"cpu": 2, "memoryMiB": 512},
    },
}


class ModuleCostConfig(BaseModel):
    """One fresh VM block contains every runtime/composition once."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    block_id: str = Field(alias="blockId", pattern=r"^[a-z][a-z0-9-]{0,39}$")
    order: list[str] = Field(default_factory=lambda: list(ARMS))

    @model_validator(mode="after")
    def complete_order(self):
        """Reject incomplete, duplicated or extra treatments."""
        if len(self.order) != len(ARMS) or set(self.order) != set(ARMS):
            raise ValueError(
                "module-cost order must contain the eight arms exactly once"
            )
        return self


def require_module_environment(environment: EnvironmentConfig) -> None:
    """Validate ownership before provider acquisition, also for direct plan callers."""
    if (
        environment.provider != "azure"
        or not environment.azure
        or environment.azure.priority != "Spot"
    ):
        raise ValueError("module-cost requires workflow-created Azure Spot machines")
    if set(environment.roles) != {"stack", "loadgen"}:
        raise ValueError("module-cost requires distinct stack and loadgen roles")
    targets = [environment.target(role) for role in ("stack", "loadgen")]
    if any(t.host or not t.name for t in targets):
        raise ValueError("module-cost machines must be created by the workflow")
    if targets[0].name == targets[1].name:
        raise ValueError("module-cost requires distinct machines")
