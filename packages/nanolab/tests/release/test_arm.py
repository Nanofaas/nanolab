from __future__ import annotations

import pytest

from nanolab.images.plan import build_image_plan
from nanolab.release import arm
from nanolab.release.model import ArtifactEvidence
from tests.conftest import source_contract_root

pytestmark = pytest.mark.nanofaas

NANOFAAS_ROOT = source_contract_root()
REGISTRY = "localhost:5000/nanofaas"


def _plan():
    return arm.build_arm64_image_plan(
        NANOFAAS_ROOT,
        "v0.18.0",
        registry=REGISTRY,
    )


def test_arm64_plan_covers_the_live_matrix_without_loss() -> None:
    """The matrix grows with the function catalog, so assert shape, not a count."""
    plan = _plan()

    assert plan.cells
    assert {cell.architecture for cell in plan.cells} == {"arm64"}
    assert len({(cell.target.name, cell.flavor) for cell in plan.cells}) == len(
        plan.cells
    )


def test_builder_bootstrap_must_explicitly_support_linux_arm64() -> None:
    arm.require_arm64_builder("Name: release\nPlatforms: linux/amd64, linux/arm64\n")

    with pytest.raises(RuntimeError, match="does not support linux/arm64"):
        arm.require_arm64_builder("Name: release\nPlatforms: linux/amd64\n")


def test_arm64_evidence_must_cover_every_planned_registry_artifact() -> None:
    plan = _plan()
    evidence = tuple(
        ArtifactEvidence("remote", f"docker://{cell.image}", "sha256:" + "a" * 64)
        for cell in plan.cells
    )

    arm.require_complete_arm64_evidence(plan, evidence)

    with pytest.raises(RuntimeError, match=f"does not cover all {len(plan.cells)}"):
        arm.require_complete_arm64_evidence(plan, evidence[:-1])


def test_server_smokes_cover_every_non_watchdog_artifact_and_health_endpoint() -> None:
    plan = _plan()
    smokes = arm.server_smoke_specs(plan)

    assert len(smokes) == sum(cell.target.name != "watchdog" for cell in plan.cells)
    assert all(smoke.cell.target.name != "watchdog" for smoke in smokes)
    control_planes = [
        smoke for smoke in smokes if smoke.cell.target.name == "control-plane"
    ]
    assert {(smoke.container_port, smoke.health_path) for smoke in control_planes} == {
        (8081, "/actuator/health")
    }
    assert all(
        (smoke.container_port, smoke.health_path) == (8080, "/health")
        for smoke in smokes
        if smoke.cell.target.name != "control-plane"
    )


def test_watchdog_smoke_accepts_only_the_expected_missing_child_exit() -> None:
    arm.require_expected_watchdog_exit(
        1,
        "",
        "Failed to spawn runtime: No such file or directory (os error 2)",
    )

    with pytest.raises(RuntimeError, match="exec format error"):
        arm.require_expected_watchdog_exit(1, "", "exec format error")
    with pytest.raises(RuntimeError, match="unexpectedly succeeded"):
        arm.require_expected_watchdog_exit(0, "nanofaas-watchdog 0.18.0", "")
    with pytest.raises(RuntimeError, match="expected missing-child"):
        arm.require_expected_watchdog_exit(1, "", "unrelated failure")


def test_arm64_plan_is_the_arm_partition_of_the_complete_image_plan() -> None:
    complete = build_image_plan(NANOFAAS_ROOT, "v0.18.0", registry=REGISTRY)

    assert _plan().cells == tuple(
        cell for cell in complete.cells if cell.architecture == "arm64"
    )
