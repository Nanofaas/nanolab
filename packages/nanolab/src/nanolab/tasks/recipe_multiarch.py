"""Multiarch publication evidence, separate from local image distributions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from nanolab.tasks.recipe import _object, _string, _verify_recipe_identity

MULTIARCH_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})


def sha256_digest(value: object) -> str:
    """Require a complete lowercase SHA-256 registry identity."""
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Malformed sha256 digest")
    return value


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse JSON keys that would otherwise silently overwrite evidence."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class MultiarchImage:
    """Published index and its executable platform manifests."""

    reference: str
    status: str
    digest: str
    platforms: tuple[str, ...]
    manifests: dict[str, str]
    provenance: bool


@dataclass(frozen=True, slots=True)
class MultiarchComponent:
    """Component metadata with no local image identity."""

    kind: str
    name: str
    sdk: str
    mode: str | None
    image: MultiarchImage
    variant: str | None
    optimization: str | None
    native: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class MultiarchDistribution:
    """Checked publication before registry verification and host projection."""

    report: Path
    recipe_sha256: str
    tag: str
    source: dict[str, object]
    modules: tuple[str, ...]
    components: tuple[MultiarchComponent, ...]


def read_multiarch_distribution(
    report: Path, *, recipe: Path, tag: str, expected_source: dict[str, object]
) -> MultiarchDistribution:
    """Validate publication identity without requiring or inventing local IDs."""
    try:
        data = _object(
            json.loads(report.read_bytes(), object_pairs_hook=unique_json_object),
            "report",
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read multiarch distribution {report}") from error
    profile = _object(yaml.safe_load(recipe.read_text()), "profile")
    registry = _object(profile.get("registry"), "registry")
    platforms = registry.get("platforms")
    if (
        not isinstance(platforms, list)
        or len(platforms) != 2
        or set(platforms) != MULTIARCH_PLATFORMS
    ):
        raise ValueError("Multiarch recipe requires linux/amd64 and linux/arm64")
    if registry.get("provenance", False) is not False:
        raise ValueError("Multiarch recipe provenance is not supported")
    if type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 2:
        raise ValueError("Unsupported multiarch distribution schemaVersion")
    expected_sha = hashlib.sha256(recipe.read_bytes()).hexdigest()
    if (
        _object(data.get("recipe"), "recipe").get("sha256") != expected_sha
        or data.get("tag") != tag
    ):
        raise ValueError("Multiarch recipe hash or tag differs from run")
    source = _object(data.get("source"), "source")
    if type(source.get("dirty")) is not bool or any(
        source.get(key) != expected_source.get(key) for key in ("revision", "dirty")
    ):
        raise ValueError("Multiarch source differs from captured inputs")
    _string(source.get("revision"), "source revision")
    modules = data.get("modules")
    if not isinstance(modules, list) or not all(
        isinstance(item, str) for item in modules
    ):
        raise ValueError("Multiarch modules are invalid")
    rows = data.get("components")
    if not isinstance(rows, list):
        raise ValueError("Multiarch components are invalid")
    components: list[MultiarchComponent] = []
    keys: set[tuple[str, str, str]] = set()
    references: set[str] = set()
    for row in rows:
        item = _object(row, "component")
        key = (
            _string(item.get("kind"), "kind"),
            _string(item.get("name"), "name"),
            _string(item.get("sdk"), "sdk"),
        )
        image = _object(item.get("image"), "image")
        reference = _string(image.get("reference"), "image reference")
        if key in keys or reference in references:
            raise ValueError("Duplicate multiarch component or image reference")
        keys.add(key)
        references.add(reference)
        selected = image.get("platforms")
        if (
            not isinstance(selected, list)
            or len(selected) != 2
            or set(selected) != set(platforms)
        ):
            raise ValueError("Multiarch image platforms differ from recipe")
        manifests = _object(image.get("manifests"), "image manifests")
        if set(manifests) != set(platforms):
            raise ValueError("Multiarch manifest platforms differ from recipe")
        if (
            image.get("status") != "published"
            or image.get("provenance") is not False
            or "id" in image
        ):
            raise ValueError(
                "Multiarch image must be published without provenance or local ID"
            )
        components.append(
            MultiarchComponent(
                kind=key[0],
                name=key[1],
                sdk=key[2],
                mode=item.get("mode"),
                image=MultiarchImage(
                    reference,
                    "published",
                    sha256_digest(image.get("digest")),
                    tuple(selected),
                    {
                        platform: sha256_digest(digest)
                        for platform, digest in manifests.items()
                    },
                    False,
                ),
                variant=item.get("variant"),
                optimization=item.get("optimization"),
                native=_object(item["native"], "native") if "native" in item else None,
            )
        )
    _verify_recipe_identity(recipe, tag, modules, components)
    return MultiarchDistribution(
        report, expected_sha, tag, source, tuple(modules), tuple(components)
    )
