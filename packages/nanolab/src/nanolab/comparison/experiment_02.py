"""Experiment 02 matrix data: configurations, cells, order and identity.

A cell of experiment 02 is one configuration, under one budget, in one
repetition. A configuration is a complete deployment: a nanoFaaS composition,
a baseline platform, or the function alone with no platform in front of it.
The budget is the deployment's CPU quota and memory limit, so the same
configuration under two budgets is two cells, not one cell measured twice.

The order is shuffled per repetition with a seed the protocol declares. A grid
of configurations and budgets is too large for hand-written orders like
experiment 01's, and running each configuration's budgets back to back would
make "which budget" and "when it ran" the same axis. The seed makes the
shuffle reproducible, which is what a resume needs.

Receipts, the ledger and the verdicts are experiment 01's. Only the evidence a
measured cell must leave behind differs: cgroup samples instead of a
Prometheus snapshot.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nanolab.comparison.experiment_01 import EVENTS, SUMMARY, identity_sha256
from nanolab.config.experiment_02 import Budget, Experiment02Protocol

Platform = Literal["nanofaas", "direct"]

SAMPLES = "metrics/cgroup-samples.jsonl"
EXPERIMENT_02_EVIDENCE: tuple[str, ...] = (SUMMARY, EVENTS, SAMPLES)
EXPERIMENT_02_ORDER_POLICY = "experiment-02-seeded-shuffle-v1"


@dataclass(frozen=True, slots=True)
class Configuration:
    """One complete deployment the experiment measures."""

    key: str
    label: str
    platform: Platform
    profile: Literal["P0", "P1"] | None = None
    runtime: Literal["jvm", "native"] | None = None
    # The recipe that builds it, for configurations prepare must publish.
    recipe: str | None = None


EXPERIMENT_02_CONFIGURATIONS: tuple[Configuration, ...] = (
    # First, because its recipe publishes the function every configuration uses.
    Configuration(
        "nanofaas-p1-jvm", "nanoFaaS P1, JVM", "nanofaas", "P1", "jvm",
        "experiment-02-p1-jvm",
    ),
    Configuration(
        "nanofaas-p1-native", "nanoFaaS P1, native", "nanofaas", "P1", "native",
        "experiment-02-p1-native",
    ),
    Configuration(
        "nanofaas-p0-jvm", "nanoFaaS P0, JVM", "nanofaas", "P0", "jvm",
        "experiment-02-p0-jvm",
    ),
    Configuration(
        "nanofaas-p0-native", "nanoFaaS P0, native", "nanofaas", "P0", "native",
        "experiment-02-p0-native",
    ),
    Configuration("direct-reference", "Function without a platform", "direct"),
)  # fmt: skip
CONFIGURATIONS_BY_KEY = {
    configuration.key: configuration for configuration in EXPERIMENT_02_CONFIGURATIONS
}
EXPERIMENT_02_PUBLISHER = EXPERIMENT_02_CONFIGURATIONS[0].key


@dataclass(frozen=True, slots=True)
class Experiment02Cell:
    """One configuration, under one budget, in one repetition."""

    configuration: Configuration
    budget: Budget
    repetition: int

    @property
    def key(self) -> str:
        """Return the configuration this cell measures."""
        return self.configuration.key

    @property
    def label(self) -> str:
        """Return the name this cell takes in the manifest."""
        return f"{self.key} {self.budget.label} run {self.repetition}"

    def run_dir(self, root: Path) -> Path:
        """Return the directory this cell's run writes its artefacts to."""
        return root / self.key / self.budget.label / f"run-{self.repetition}"


def build_experiment_02_matrix(
    configurations: tuple[Configuration, ...],
    budgets: tuple[Budget, ...],
    repetitions: int,
    *,
    seed: int,
) -> tuple[Experiment02Cell, ...]:
    """Every (configuration, budget) pair once per repetition, shuffled per repetition.

    A string seed is hashed the same way on every run, so the order is
    reproducible from the protocol alone.
    """
    if repetitions < 1:
        raise ValueError("experiment-02 needs at least one repetition")
    if not configurations:
        raise ValueError("experiment-02 needs at least one configuration")
    if not budgets:
        raise ValueError("experiment-02 needs at least one budget")
    cells: list[Experiment02Cell] = []
    for repetition in range(1, repetitions + 1):
        pairs = [
            (configuration, budget)
            for configuration in configurations
            for budget in budgets
        ]
        order = random.Random(f"{seed}:{repetition}")  # nosec B311 - not a secret
        order.shuffle(pairs)
        cells.extend(
            Experiment02Cell(configuration, budget, repetition)
            for configuration, budget in pairs
        )
    return tuple(cells)


def experiment_02_identity(
    protocol: Experiment02Protocol,
    cells: tuple[Experiment02Cell, ...],
    *,
    payload: bytes,
    script: Path,
    selected: tuple[str, ...],
    seed: int,
) -> dict[str, object]:
    """Return what the manifest records about experiment 02 beyond the scenario."""
    values = protocol.model_dump(mode="json", by_alias=True)
    return {
        "profile": "experiment-02",
        "orderPolicy": EXPERIMENT_02_ORDER_POLICY,
        "seed": seed,
        "order": [cell.label for cell in cells],
        "protocol": values,
        "protocolSha256": identity_sha256(values),
        "payload": {
            "file": protocol.payload_file,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        },
        "script": {
            "name": script.name,
            "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        },
        "configurations": {
            key: {
                "label": CONFIGURATIONS_BY_KEY[key].label,
                "platform": CONFIGURATIONS_BY_KEY[key].platform,
                "profile": CONFIGURATIONS_BY_KEY[key].profile,
                "runtime": CONFIGURATIONS_BY_KEY[key].runtime,
                "recipe": CONFIGURATIONS_BY_KEY[key].recipe,
            }
            for key in selected
        },
        "budgets": [budget.label for budget in protocol.budgets],
        "subset": set(selected) != set(CONFIGURATIONS_BY_KEY),
    }
