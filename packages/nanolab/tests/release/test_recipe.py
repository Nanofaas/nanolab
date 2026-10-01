"""Release recipes must preserve the guarded matrix before acquisition."""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from nanolab.images.plan import build_image_plan
from nanolab.release import recipe

SOURCE = Path(os.environ["NANOFAAS_ROOT"])
PROFILES = Path(__file__).resolve().parents[2] / "recipes"
MODULES = (
    "async-queue",
    "autoscaler",
    "build-metadata",
    "concurrency-control",
    "k8s-deployment-provider",
    "offload",
    "runtime-config",
    "sync-queue",
)


def _groups(profiles: Path = PROFILES):
    plan = build_image_plan(SOURCE, "v9.9.9", architectures=("amd64",))
    return recipe.prepare_release_recipe_groups(SOURCE, plan, profiles_root=profiles)


def test_release_profiles_cover_exact_guarded_matrix() -> None:
    groups = _groups()
    assert tuple(g.flavor for g in groups) == ("jvm", "native", "default")
    assert tuple(len(g.cells) for g in groups) == (9, 12, 23)
    images = [c.image for g in groups for c in g.cells]
    assert len(images) == len(set(images)) == 44
    assert "127.0.0.1:5000/nanofaas/java-lite-word-stats:v9.9.9-amd64-native" in images
    assert "127.0.0.1:5000/nanofaas/java-warm-echo:v9.9.9-amd64-jvm" in images
    assert "127.0.0.1:5000/nanofaas/watchdog:v9.9.9-amd64" in images
    assert all(g.modules == MODULES for g in groups)
    default = yaml.safe_load(groups[2].profile_bytes)
    assert "container" not in default["controlPlane"]
    assert default["controlPlane"]["build"]["mode"] == "jvm"
    native = yaml.safe_load(groups[1].profile_bytes)
    assert native["controlPlane"]["build"]["variant"] == "native-o3-g1"
    assert native["controlPlane"]["build"]["native"] == {
        "optimization": "3",
        "gc": "G1",
    }
    lite = [f for f in native["functions"] if f["sdk"] == "java-lite"]
    assert len(lite) == 3
    assert all(
        f["build"]["native"] == {"optimization": "3", "gc": "serial"} for f in lite
    )


@pytest.fixture
def profiles(tmp_path: Path) -> Path:
    destination = tmp_path / "profiles"
    destination.mkdir()
    for flavor in ("jvm", "native", "default"):
        name = f"release-amd64-{flavor}.yaml"
        shutil.copyfile(PROFILES / name, destination / name)
    return destination


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "duplicate",
        "sdk",
        "service",
        "repository",
        "tag",
        "mode",
        "variant",
        "jvm",
        "native",
        "modules",
        "platforms",
        "provenance",
        "configuration",
        "unknown",
    ],
)
def test_release_profile_drift_fails_before_acquisition(
    profiles: Path, mutation: str
) -> None:
    path = profiles / "release-amd64-jvm.yaml"
    data = yaml.safe_load(path.read_text())
    if mutation == "missing":
        data["functions"].pop()
    elif mutation in {"extra", "duplicate"}:
        item = dict(data["functions"][0])
        if mutation == "extra":
            item["name"] = "foreign"
        data["functions"].append(item)
    elif mutation == "sdk":
        data["functions"][0]["sdk"] = "java-lite"
    elif mutation == "service":
        data["services"][0]["name"] = "word-stats"
    elif mutation in {"repository", "tag"}:
        data["registry"][mutation] = (
            "invalid/value" if mutation == "tag" else "other.example/nanofaas"
        )
    elif mutation == "mode":
        data["functions"][0]["build"]["mode"] = "native"
    elif mutation == "variant":
        data["controlPlane"]["build"]["variant"] = "jvm-c1"
    elif mutation == "jvm":
        data["functions"][0]["jvm"]["args"] = ["-XX:+UseSerialGC"]
    elif mutation == "native":
        path = profiles / "release-amd64-native.yaml"
        data = yaml.safe_load(path.read_text())
        data["functions"][0]["build"]["native"]["gc"] = "serial"
    elif mutation == "modules":
        data["controlPlane"]["modules"].append("container-deployment-provider")
    elif mutation in {"platforms", "provenance"}:
        data["registry"][mutation] = (
            ["linux/amd64"] if mutation == "platforms" else False
        )
    elif mutation == "configuration":
        data["controlPlane"]["config"] = {"nanofaas": {"metrics": {"profile": "soak"}}}
    else:
        data["undeclared"] = True
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match=r"(?i)release recipe"):
        _groups(profiles)


def test_profile_freeze_uses_raw_bytes(profiles: Path) -> None:
    groups = _groups(profiles)
    path = profiles / "release-amd64-jvm.yaml"
    raw = path.read_bytes()
    path.write_bytes(raw + b"\n# Changed input identity\n")
    assert groups[0].profile_bytes == raw
    assert groups[0].profile_digest == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert _groups(profiles)[0].profile_digest != groups[0].profile_digest


def test_matrix_catalog_change_fails_preflight() -> None:
    plan = build_image_plan(SOURCE, "v9.9.9", architectures=("amd64",))
    changed = replace(plan, cells=plan.cells[:-1])
    with pytest.raises(ValueError, match="matrix"):
        recipe.prepare_release_recipe_groups(SOURCE, changed, profiles_root=PROFILES)


def test_module_catalog_change_fails_preflight(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(SOURCE / "platform/modules", source / "platform/modules")
    new = source / "platform/modules/another-module"
    new.mkdir()
    (new / "module.properties").write_text(
        "id=another-module\ndefaultEnabled=false\nconflicts=\n"
    )
    plan = build_image_plan(SOURCE, "v9.9.9", architectures=("amd64",))
    with pytest.raises(ValueError, match="modules"):
        recipe.prepare_release_recipe_groups(source, plan, profiles_root=PROFILES)


def test_missing_release_profile_is_a_preflight_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="profile"):
        _groups(tmp_path)
