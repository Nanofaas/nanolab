"""Experiment 02 accepts one coherent budget grid for a container loadtest."""

import copy

import pytest
import yaml
from pydantic import ValidationError

from nanolab.config.experiment_01 import experiment_01_payload
from nanolab.config.experiment_02 import Budget, Experiment02Protocol
from nanolab.config.scenario import ScenarioConfig
from nanolab.workspace.paths import discover_tool_root

SCENARIOS = discover_tool_root() / "scenarios"

GENERATOR = {
    "maxRateDeviation": 0.05,
    "maxDroppedFraction": 0.01,
    "windowSeconds": 10,
    "maxStartDelaySeconds": 2,
}


def _pilot() -> dict:
    return yaml.safe_load((SCENARIOS / "experiment-02-pilot.yaml").read_text())


def _protocol(**changes: object) -> Experiment02Protocol:
    values = copy.deepcopy(_pilot()["experiment02"])
    values.update(changes)
    return Experiment02Protocol.model_validate(values)


def _scenario(**changes: object) -> ScenarioConfig:
    values = copy.deepcopy(_pilot())
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return ScenarioConfig.model_validate(values)


def test_budget_label_names_the_run_directory():
    assert Budget(cpus=1.5, memoryMiB=1024).label == "cpu1.5-mem1024"
    assert Budget(cpus=2, memoryMiB=2048).label == "cpu2-mem2048"


def test_reference_budget_must_belong_to_the_grid():
    with pytest.raises(ValidationError, match="referenceBudget"):
        _protocol(
            budgets=[{"cpus": 2, "memoryMiB": 2048}],
            referenceBudget={"cpus": 1, "memoryMiB": 1024},
        )


def test_duplicate_budgets_are_rejected():
    with pytest.raises(ValidationError, match="unique"):
        _protocol(budgets=[{"cpus": 2, "memoryMiB": 2048}] * 2)


def test_frozen_protocol_requires_the_png_hash():
    with pytest.raises(ValidationError, match="pngSha256"):
        _protocol(status="frozen", generator=GENERATOR, pngSha256=None)
    frozen = _protocol(status="frozen", generator=GENERATOR, pngSha256="ab" * 32)
    assert frozen.png_sha256 == "ab" * 32


def test_experiment_02_is_a_container_loadtest_of_qr_code_alone():
    with pytest.raises(ValidationError, match="container loadtest"):
        _scenario(backend="k8s")
    with pytest.raises(ValidationError, match="container loadtest"):
        _scenario(functions=["word-stats-java"])
    with pytest.raises(ValidationError, match="experiment02 block"):
        _scenario(experiment02=None)


def test_pilot_preset_loads_with_its_payload():
    config = ScenarioConfig.model_validate(_pilot())
    protocol = config.experiment_02
    assert protocol is not None
    assert protocol.status == "pilot"
    assert protocol.reference_budget in protocol.budgets
    raw = experiment_01_payload(protocol, SCENARIOS / "payloads")
    assert raw == b'{"size":1024,"text":"' + b"A" * 1024 + b'"}\n'
