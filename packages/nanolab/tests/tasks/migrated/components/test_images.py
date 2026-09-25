from __future__ import annotations

from pathlib import Path

from nanolab.tasks.components.images import control_image

WORKSPACE_ROOT = Path(__file__).resolve().parents[6]
LIVE_E2E_SCENARIO_IMAGE_CONSUMERS = (
    "packages/nanolab/src/nanolab/tasks/components/images.py",
    "packages/nanolab/src/nanolab/plans/validate.py",
)
REMOVED_RELEASE_IMAGE_CLI_SYNTAX = ("--arch-suffix", "--arch multi")


def test_image_name_helpers() -> None:
    assert control_image("reg:5000") == "reg:5000/nanofaas/control-plane:e2e"


def test_live_e2e_scenario_image_consumers_do_not_use_removed_images_cli_syntax() -> (
    None
):
    violations = {
        str(path.relative_to(WORKSPACE_ROOT)): syntax
        for relative_path in LIVE_E2E_SCENARIO_IMAGE_CONSUMERS
        for path in [WORKSPACE_ROOT / relative_path]
        for syntax in REMOVED_RELEASE_IMAGE_CLI_SYNTAX
        if syntax in path.read_text(encoding="utf-8")
    }

    assert violations == {}
