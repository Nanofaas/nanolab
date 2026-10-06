"""Validated data needed to compile and execute a release."""

from dataclasses import dataclass
from pathlib import Path

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.plan import ImagePlan
from nanolab.release.model import CredentialFiles, ReleaseIdentity, ReleaseSettings
from nanolab.release.recipe import ReleaseRecipeGroup


@dataclass(frozen=True, slots=True)
class ReleaseRequest:
    """Everything needed to compile a release workflow."""

    repo_root: Path
    version: str
    environment: EnvironmentConfig
    scenario: ScenarioConfig
    image_plan: ImagePlan
    settings: ReleaseSettings
    run_dir: Path
    performance_root: Path
    source_tree: Path
    credentials: CredentialFiles | None = None
    nanofaas_root: Path | None = None  # defaults to repo_root
    identity: ReleaseIdentity | None = None
    recipe_groups: tuple[ReleaseRecipeGroup, ...] = ()
    arm_image_plan: ImagePlan | None = None
    arm_recipe_groups: tuple[ReleaseRecipeGroup, ...] = ()
    source_archive: Path | None = None
    archive_digest: str = ""
    inventory_file: Path | None = None
