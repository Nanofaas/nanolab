"""Release recipes must preserve the guarded matrix before acquisition."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from nanolab.images.plan import ImageArchitecture, build_image_plan
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


def _groups(profiles: Path = PROFILES, architecture: ImageArchitecture = "amd64"):
    plan = build_image_plan(SOURCE, "v9.9.9", architectures=(architecture,))
    if architecture == "amd64":
        return recipe.prepare_release_recipe_groups(
            SOURCE, plan, profiles_root=profiles
        )
    return recipe.prepare_release_recipe_groups(
        SOURCE, plan, profiles_root=profiles, architecture=architecture
    )


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
        raw = (PROFILES / name).read_bytes()
        (destination / name).write_bytes(raw)
        # Independent fixture for policy/byte-freeze tests; checked-in ARM
        # profiles are exercised separately by exact coverage and Gradle.
        (destination / f"release-arm64-{flavor}.yaml").write_bytes(
            raw.replace(b"amd64", b"arm64")
        )
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
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_release_profile_drift_fails_before_acquisition(
    profiles: Path, mutation: str, architecture: ImageArchitecture
) -> None:
    path = profiles / f"release-{architecture}-jvm.yaml"
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
        path = profiles / f"release-{architecture}-native.yaml"
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
        _groups(profiles, architecture)


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


def _report(group) -> dict:
    """Mirror the pinned Gradle report shape without using adapter helpers."""
    profile = yaml.safe_load(group.profile_bytes)
    rows = []
    declared = [
        (
            "control-plane",
            {"name": "control-plane", "sdk": "java", **profile["controlPlane"]},
        )
    ]
    declared.extend(("function", item) for item in profile["functions"])
    declared.extend(("service", item) for item in profile["services"])
    for index, (kind, item) in enumerate(declared):
        name, sdk = item["name"], item["sdk"]
        build = item.get("build", {})
        mode = build.get("mode", "container")
        artifact = (
            "control-plane/"
            if kind == "control-plane"
            else f"{'functions' if kind == 'function' else 'services'}/{sdk}/{name}/"
            if mode != "container"
            else None
        )
        row = {
            "kind": kind,
            "name": name,
            "sdk": sdk,
            "mode": mode,
            "artifact": artifact,
            "image": None,
        }
        if kind == "control-plane":
            row.update(
                variant="native-o3-g1" if mode == "native" else "jvm-g1-c2",
                optimization="3" if mode == "native" else "c2",
            )
        if mode == "native":
            gc = "serial" if sdk == "java-lite" else "G1"
            row["native"] = {
                "optimization": "3",
                "gc": gc,
                "monitoring": [] if sdk == "java-lite" else ["jfr"],
                "builder": "container",
                "distribution": "community" if sdk == "java-lite" else "oracle",
            }
        if "container" in item:
            row["image"] = {
                "reference": (
                    f"127.0.0.1:5000/nanofaas/{item['container']['image']}:{group.tag}"
                ),
                "status": "built",
                "id": "sha256:" + f"{index + 1:064x}",
            }
        rows.append(row)
    return {
        "schemaVersion": 2,
        "recipe": {
            "name": group.name,
            "sha256": group.profile_digest.removeprefix("sha256:"),
            "schemaVersion": 2,
        },
        "tag": group.tag,
        "source": None,
        "modules": list(MODULES),
        "components": rows,
    }


@pytest.mark.parametrize(("index", "count"), [(0, 9), (1, 12), (2, 23)])
def test_release_distribution_maps_all_component_kinds(
    tmp_path: Path, index: int, count: int
) -> None:
    group = _groups()[index]
    path = tmp_path / "distribution.json"
    path.write_text(json.dumps(_report(group)))
    components = recipe.read_release_distribution(path, group=group)
    assert len(components) == count
    assert {c.image.reference for c in components} == {
        cell.image for cell in group.cells
    }
    if index == 1:
        cp = next(c for c in components if c.kind == "control-plane")
        assert cp.variant == "native-o3-g1"
        assert cp.native == {
            "optimization": "3",
            "gc": "G1",
            "monitoring": ["jfr"],
            "builder": "container",
            "distribution": "oracle",
        }
        lite = next(c for c in components if c.sdk == "java-lite")
        assert lite.native is not None
        assert lite.native["gc"] == "serial"
        assert lite.native["distribution"] == "community"
    if index == 2:
        assert not any(c.kind == "control-plane" for c in components)
        assert any(c.sdk == "bash" and c.mode == "container" for c in components)
        assert any(c.kind == "service" and c.name == "watchdog" for c in components)


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "recipe-schema",
        "hash",
        "name",
        "tag",
        "modules",
        "missing-source",
        "duplicate",
        "extra",
        "missing",
        "foreign-image",
        "missing-id",
        "malformed-id",
        "failed",
        "published",
        "digest",
        "mode",
        "variant",
        "optimization",
        "distribution",
        "gc",
        "jfr",
        "native-optimization",
        "builder",
        "null-image",
        "missing-image",
        "artifact",
        "unknown-metadata",
        "source",
    ],
)
def test_release_distribution_rejects_identity_or_matrix_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    group = _groups()[1]
    data = _report(group)
    cp = data["components"][0]
    if mutation == "schema":
        data["schemaVersion"] = 1
    elif mutation == "recipe-schema":
        data["recipe"]["schemaVersion"] = 1
    elif mutation == "hash":
        data["recipe"]["sha256"] = "0" * 64
    elif mutation == "name":
        data["recipe"]["name"] = "foreign"
    elif mutation == "tag":
        data["tag"] = "old"
    elif mutation == "modules":
        data["modules"].append("async-queue")
    elif mutation == "missing-source":
        del data["source"]
    elif mutation == "duplicate":
        data["components"].append(cp)
    elif mutation == "extra":
        data["components"].append({**cp, "name": "foreign"})
    elif mutation == "missing":
        data["components"].pop()
    elif mutation == "foreign-image":
        cp["image"]["reference"] = "foreign/image:old"
    elif mutation == "missing-id":
        del cp["image"]["id"]
    elif mutation == "malformed-id":
        cp["image"]["id"] = "sha256:abcd"
    elif mutation in {"failed", "published"}:
        cp["image"]["status"] = mutation
    elif mutation == "digest":
        cp["image"]["digest"] = "sha256:" + "a" * 64
    elif mutation == "mode":
        cp["mode"] = "jvm"
    elif mutation == "variant":
        cp["variant"] = "native-os"
    elif mutation == "optimization":
        cp["optimization"] = "s"
    elif mutation == "distribution":
        cp["native"]["distribution"] = "community"
    elif mutation == "gc":
        cp["native"]["gc"] = "serial"
    elif mutation == "jfr":
        cp["native"]["monitoring"] = []
    elif mutation == "native-optimization":
        cp["native"]["optimization"] = "s"
    elif mutation == "builder":
        cp["native"]["builder"] = "host"
    elif mutation == "null-image":
        cp["image"] = None
    elif mutation == "missing-image":
        del cp["image"]
    elif mutation == "artifact":
        cp["artifact"] = "../../foreign"
    elif mutation == "unknown-metadata":
        cp["foreign"] = True
    else:
        data["source"] = {"revision": "synthetic", "dirty": False}
    path = tmp_path / "distribution.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=r"(?i)recipe|sha256|json"):
        recipe.read_release_distribution(path, group=group)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-cp",
        "missing-image",
        "cp-image",
        "cp-native",
        "cp-mode",
        "null-function",
    ],
)
def test_artifact_only_control_plane_is_explicitly_validated(
    tmp_path: Path, mutation: str
) -> None:
    group = _groups()[2]
    data = _report(group)
    cp = data["components"][0]
    if mutation == "missing-cp":
        data["components"].pop(0)
    elif mutation == "missing-image":
        del cp["image"]
    elif mutation == "cp-image":
        cp["image"] = {
            "reference": "extra/image",
            "id": "sha256:" + "a" * 64,
            "status": "built",
        }
    elif mutation == "cp-native":
        cp["native"] = {"gc": "serial"}
    elif mutation == "cp-mode":
        cp["mode"] = "native"
    else:
        data["components"][1]["image"] = None
    path = tmp_path / "distribution.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=r"(?i)recipe|sha256|json"):
        recipe.read_release_distribution(path, group=group)


@pytest.mark.parametrize(
    "body", ['{"schemaVersion":2,"schemaVersion":2}', '{"schemaVersion":', "{}"]
)
def test_malformed_release_report_fails(tmp_path: Path, body: str) -> None:
    path = tmp_path / "distribution.json"
    path.write_text(body)
    with pytest.raises(ValueError, match=r"(?i)recipe|sha256|json"):
        recipe.read_release_distribution(path, group=_groups()[0])


def test_null_archive_source_is_not_commit_evidence(tmp_path: Path) -> None:
    group = _groups()[0]
    path = tmp_path / "distribution.json"
    data = _report(group)
    path.write_text(json.dumps(data))
    assert len(recipe.read_release_distribution(path, group=group)) == 9
    data["source"] = {"revision": "a" * 40, "dirty": False}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="source"):
        recipe.read_release_distribution(path, group=group)
    assert (
        len(recipe.read_release_distribution(path, group=group, source_commit="a" * 40))
        == 9
    )


def test_release_profiles_cover_both_architectures():
    amd = _groups()
    arm = _groups(architecture="arm64")
    for architecture, groups in (("amd64", amd), ("arm64", arm)):
        assert tuple(len(g.cells) for g in groups) == (9, 12, 23)
        assert all(g.modules == MODULES for g in groups)
        assert all(c.architecture == architecture for g in groups for c in g.cells)
        assert [g.tag for g in groups] == [
            f"v9.9.9-{architecture}-jvm",
            f"v9.9.9-{architecture}-native",
            f"v9.9.9-{architecture}",
        ]
        assert all(c.image.endswith(":" + g.tag) for g in groups for c in g.cells)
        native = yaml.safe_load(groups[1].profile_bytes)
        assert native["controlPlane"]["build"]["variant"] == "native-o3-g1"
        assert native["controlPlane"]["build"]["native"] == {
            "optimization": "3",
            "gc": "G1",
        }
        assert all(
            f["build"]["native"] == {"optimization": "3", "gc": "serial"}
            for f in native["functions"]
            if f["sdk"] == "java-lite"
        )
        assert (
            "container" not in yaml.safe_load(groups[2].profile_bytes)["controlPlane"]
        )
    images = [c.image for g in (*amd, *arm) for c in g.cells]
    assert len(images) == len(set(images)) == 88
    assert "127.0.0.1:5000/nanofaas/java-lite-word-stats:v9.9.9-arm64-native" in images
    assert "127.0.0.1:5000/nanofaas/java-warm-echo:v9.9.9-arm64-jvm" in images
    assert "127.0.0.1:5000/nanofaas/watchdog:v9.9.9-arm64" in images


def test_arm_profile_freeze_uses_raw_bytes(profiles):
    groups = _groups(profiles, "arm64")
    path = profiles / "release-arm64-jvm.yaml"
    raw = path.read_bytes()
    path.write_bytes(raw + b"\n# New raw input identity\n")
    assert groups[0].profile_bytes == raw
    assert groups[0].profile_digest == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert _groups(profiles, "arm64")[0].profile_digest != groups[0].profile_digest


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_recipe_group_rejects_wrong_or_mixed_architecture(architecture):
    plan = build_image_plan(SOURCE, "v9.9.9", architectures=("amd64", "arm64"))
    with pytest.raises(ValueError, match=r"matrix|architecture"):
        recipe.prepare_release_recipe_groups(
            SOURCE, plan, profiles_root=PROFILES, architecture=architecture
        )
