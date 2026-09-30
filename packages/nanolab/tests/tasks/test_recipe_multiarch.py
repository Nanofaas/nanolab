from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from nanolab.tasks.recipe import read_distribution

PLATFORMS = ["linux/amd64", "linux/arm64"]
SOURCE = {"revision": "123", "dirty": False}
INDEX = "sha256:" + "a" * 64
MANIFESTS = {"linux/amd64": "sha256:" + "b" * 64, "linux/arm64": "sha256:" + "c" * 64}


def multiarch_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    recipe = tmp_path / "recipe.yaml"
    profile = {
        "schemaVersion": 2,
        "name": "multiarch",
        "registry": {
            "repository": "127.0.0.1:5000/nanofaas",
            "tag": "base",
            "platforms": PLATFORMS,
            "provenance": False,
        },
        "controlPlane": {
            "modules": ["build-metadata", "container-deployment-provider"],
            "build": {"mode": "jvm", "variant": "multiarch"},
            "container": {"image": "control-plane"},
        },
        "functions": [
            {
                "name": "word-stats",
                "sdk": "java",
                "build": {"mode": "jvm"},
                "container": {"image": "word-stats-java"},
            }
        ],
    }
    recipe.write_text(yaml.safe_dump(profile))
    data: dict[str, Any] = {
        "schemaVersion": 2,
        "recipe": {
            "name": "multiarch",
            "sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
        },
        "tag": "run-1",
        "source": dict(SOURCE),
        "modules": profile["controlPlane"]["modules"],
        "components": [],
    }
    for kind, name, image in [
        ("control-plane", "control-plane", "control-plane"),
        ("function", "word-stats", "word-stats-java"),
    ]:
        item = {
            "kind": kind,
            "name": name,
            "sdk": "java",
            "mode": "jvm",
            "image": {
                "reference": f"127.0.0.1:5000/nanofaas/{image}:run-1",
                "status": "published",
                "digest": INDEX,
                "platforms": PLATFORMS.copy(),
                "manifests": dict(MANIFESTS),
                "provenance": False,
            },
        }
        if kind == "control-plane":
            item.update(variant="multiarch", optimization="c2")
        data["components"].append(item)
    report = tmp_path / "distribution.json"
    report.write_text(json.dumps(data))
    return recipe, report, data


def test_read_multiarch_distribution_without_local_id(tmp_path: Path) -> None:
    from nanolab.tasks.recipe_multiarch import read_multiarch_distribution

    recipe, report, _ = multiarch_fixture(tmp_path)
    result = read_multiarch_distribution(
        report, recipe=recipe, tag="run-1", expected_source=SOURCE
    )
    assert result.source == SOURCE
    assert len(result.components) == 2
    for component in result.components:
        assert component.image.platforms == ("linux/amd64", "linux/arm64")
        assert component.image.digest == INDEX
        assert component.image.manifests == MANIFESTS
        assert not hasattr(component.image, "id")


@pytest.mark.parametrize(
    "failure",
    [
        "hash",
        "tag",
        "source",
        "dirty-type",
        "modules",
        "kind",
        "sdk",
        "mode",
        "variant",
        "optimization",
        "duplicate",
        "reference",
        "platform-duplicate",
        "platform-missing",
        "platform-extra",
        "manifest-missing",
        "manifest-extra",
        "digest",
        "child-digest",
        "provenance",
        "status",
        "schema",
        "empty",
    ],
)
def test_reject_inconsistent_multiarch_report(tmp_path: Path, failure: str) -> None:
    from nanolab.tasks.recipe_multiarch import read_multiarch_distribution

    recipe, report, data = multiarch_fixture(tmp_path)
    component = data["components"][0]
    image = component["image"]
    if failure == "hash":
        data["recipe"]["sha256"] = "wrong"
    elif failure == "tag":
        data["tag"] = "other"
    elif failure == "source":
        data["source"]["revision"] = "other"
    elif failure == "dirty-type":
        data["source"]["dirty"] = 0
    elif failure == "modules":
        data["modules"].append("unexpected")
    elif failure in {"kind", "sdk", "mode", "variant", "optimization"}:
        component[failure] = "wrong"
    elif failure == "duplicate":
        data["components"].append(component)
    elif failure == "reference":
        data["components"][1]["image"]["reference"] = image["reference"]
    elif failure == "platform-duplicate":
        image["platforms"].append("linux/amd64")
    elif failure == "platform-missing":
        image["platforms"].pop()
    elif failure == "platform-extra":
        image["platforms"].append("linux/s390x")
    elif failure == "manifest-missing":
        image["manifests"].pop("linux/amd64")
    elif failure == "manifest-extra":
        image["manifests"]["linux/s390x"] = INDEX
    elif failure == "digest":
        image["digest"] = "sha256:abc"
    elif failure == "child-digest":
        image["manifests"]["linux/amd64"] = "sha256:abc"
    elif failure == "provenance":
        image["provenance"] = True
    elif failure == "status":
        image["status"] = "published-unverified"
    elif failure == "schema":
        data["schemaVersion"] = True
    elif failure == "empty":
        data["components"] = []
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=r"Multiarch|multiarch|Recipe|recipe|sha256"):
        read_multiarch_distribution(
            report, recipe=recipe, tag="run-1", expected_source=SOURCE
        )


def test_reject_duplicate_json_platform_key(tmp_path: Path) -> None:
    from nanolab.tasks.recipe_multiarch import read_multiarch_distribution

    recipe, report, _ = multiarch_fixture(tmp_path)
    report.write_text(
        report.read_text().replace(
            '"linux/amd64":',
            '"linux/amd64": "sha256:' + "d" * 64 + '", "linux/amd64":',
            1,
        )
    )
    with pytest.raises(ValueError, match="Duplicate"):
        read_multiarch_distribution(
            report, recipe=recipe, tag="run-1", expected_source=SOURCE
        )


def test_local_reader_still_rejects_multiarch(tmp_path: Path) -> None:
    recipe, report, _ = multiarch_fixture(tmp_path)
    with pytest.raises(ValueError, match="Multi-platform"):
        read_distribution(report, recipe=recipe, tag="run-1", published=True)
