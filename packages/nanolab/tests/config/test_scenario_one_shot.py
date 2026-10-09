"""Parse-time guards for the three independent one-shot workflows."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanolab.config.scenario import ScenarioConfig


def scenario(workflow: str) -> dict:
    """Build an explicit two-edge and terminal-cloud local topology."""
    return {
        "workflow": workflow,
        "backend": "container",
        "functions": ["one-shot-workload-rust"],
        "oneShot": {
            "schemaVersion": 1,
            "provider": "multipass",
            "purpose": "workflow-validation",
            "nodes": [
                {"id": "edge-0", "kind": "edge"},
                {"id": "edge-1", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "functions": {"one-shot-workload-rust": {"input": {"seed": 7}}},
        },
    }


@pytest.mark.parametrize(
    "workflow",
    ["one-shot-calibration", "one-shot-qualification", "one-shot-experiment"],
)
def test_recognizes_three_workflows_with_separate_artifacts(workflow: str) -> None:
    """Qualification consumes service data; experiments consume both artifacts."""
    data = scenario(workflow)
    if workflow != "one-shot-calibration":
        data["oneShot"]["profile"] = {"path": "profile.json", "sha256": "a" * 64}
    if workflow == "one-shot-experiment":
        data["oneShot"]["qualification"] = {"path": "timing.json", "sha256": "b" * 64}
    assert ScenarioConfig.model_validate(data).workflow == workflow


@pytest.mark.parametrize("workflow", ["one-shot-qualification", "one-shot-experiment"])
def test_workflow_cannot_recalibrate_implicitly(workflow: str) -> None:
    """Missing prerequisite artifacts fail before resources are acquired."""
    with pytest.raises(ValidationError, match="profile"):
        ScenarioConfig.model_validate(scenario(workflow))
    data = scenario(workflow)
    data["oneShot"]["profile"] = {"path": "profile.json", "sha256": "a" * 64}
    if workflow == "one-shot-experiment":
        with pytest.raises(ValidationError, match="qualification"):
            ScenarioConfig.model_validate(data)


def test_one_shot_rejects_duplicate_nodes_unknown_options_and_other_providers() -> None:
    """An ambiguous topology or ignored option must never start a run."""
    for patch in ({"provider": "azure"}, {"unexpected": 1}, {"schemaVersion": 2}):
        data = scenario("one-shot-calibration")
        data["oneShot"].update(patch)
        with pytest.raises(ValidationError):
            ScenarioConfig.model_validate(data)
    data = scenario("one-shot-calibration")
    data["oneShot"]["nodes"][1]["id"] = "edge-0"
    with pytest.raises(ValidationError, match="unique"):
        ScenarioConfig.model_validate(data)


def test_optimizer_pool_must_fit_at_least_one_replica_per_function():
    data = scenario("one-shot-calibration")
    data["oneShot"]["functions"]["one-shot-workload-rust"]["memoryMiB"] = 1024
    with pytest.raises(ValidationError, match="one replica"):
        ScenarioConfig.model_validate(data)


def test_declared_replica_cap_covers_the_entire_optimizer_memory_pool():
    data = scenario("one-shot-calibration")
    data["oneShot"]["nodes"][0]["memoryCapacityMiB"] = 512
    data["oneShot"]["functions"]["one-shot-workload-rust"]["maxReplicas"] = 2
    with pytest.raises(ValidationError, match="replica cap"):
        ScenarioConfig.model_validate(data)
