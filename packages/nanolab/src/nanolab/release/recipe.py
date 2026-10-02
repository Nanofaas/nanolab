"""Frozen recipe inputs that preserve the guarded release image matrix."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from nanolab.images.plan import (
    DEFAULT_REGISTRY,
    ImageArchitecture,
    ImageCell,
    ImageFlavor,
    ImagePlan,
)
from nanolab.tasks.recipe import RecipeComponent, RecipeImage, _object, _string
from nanolab.tasks.recipe_multiarch import sha256_digest, unique_json_object


@dataclass(frozen=True, slots=True)
class ReleaseRecipeGroup:
    """One immutable tag group bound to its complete expected image cells."""

    flavor: ImageFlavor
    name: str
    profile_bytes: bytes
    profile_digest: str
    tag: str
    cells: tuple[ImageCell, ...]
    modules: tuple[str, ...]


class _ProfileLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate release recipe YAML key")
        return super().construct_mapping(node, deep=deep)


def release_modules(source_tree: Path) -> tuple[str, ...]:
    """Resolve the guarded catalog's legacy all selection, including conflicts."""
    descriptors: dict[str, dict[str, str]] = {}
    for file in sorted((source_tree / "platform/modules").glob("*/module.properties")):
        properties = dict(
            line.split("=", 1)
            for line in file.read_text().splitlines()
            if line.strip() and not line.startswith("#") and "=" in line
        )
        if properties.get("id") != file.parent.name or properties.get(
            "defaultEnabled"
        ) not in {"true", "false"}:
            raise ValueError("Invalid guarded release module descriptor")
        descriptors[file.parent.name] = properties
    if not descriptors:
        raise ValueError("Missing guarded release modules")

    def conflicts(left: str, right: str) -> bool:
        return right in descriptors[left].get("conflicts", "").split(
            ","
        ) or left in descriptors[right].get("conflicts", "").split(",")

    defaults = {
        name for name, props in descriptors.items() if props["defaultEnabled"] == "true"
    }
    candidates = set(descriptors) - defaults
    candidates = {
        name
        for name in candidates
        if not any(conflicts(name, default) for default in defaults)
    }
    for priority in (defaults, candidates):
        if any(
            conflicts(left, right)
            for left in priority
            for right in priority
            if left != right
        ):
            raise ValueError("Guarded release modules conflict at equal priority")
    selected = defaults | candidates
    for name in selected:
        props = descriptors[name]
        strong = set(filter(None, props.get("requires.strong", "").split(",")))
        one_of = set(filter(None, props.get("requires.oneOf", "").split(",")))
        if not strong <= selected or (one_of and not one_of & selected):
            raise ValueError("Guarded release modules have unsatisfied constraints")
    return tuple(sorted(selected))


def _build(mode: str, *, lite: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": mode}
    if mode == "native":
        result.update(
            builder="container",
            native={"optimization": "3", "gc": "serial" if lite else "G1"},
        )
    return result


def _control(flavor: ImageFlavor, modules: tuple[str, ...]) -> dict[str, Any]:
    native = flavor == "native"
    build = _build("native" if native else "jvm")
    build["variant"] = "native-o3-g1" if native else "jvm-g1-c2"
    result: dict[str, Any] = {"modules": list(modules), "build": build}
    if not native:
        result["jvm"] = {"args": ["-XX:+UseG1GC"]}
    if flavor != "default":
        result["container"] = {"image": "control-plane"}
    return result


def _component(cell: ImageCell) -> tuple[str, dict[str, Any]]:
    image = cell.target.name
    if image == "java-warm-echo":
        field, name, sdk = "services", "warm-echo", "java"
    elif image == "watchdog":
        field, name, sdk = "services", "watchdog", "dockerfile"
    else:
        sdk = next(
            (
                sdk
                for sdk in ("java-lite", "java", "bash", "go", "javascript", "python")
                if image.startswith(sdk + "-")
            ),
            "",
        )
        if not sdk:
            raise ValueError(f"Unsupported release recipe target {image}")
        field, name = "functions", image[len(sdk) + 1 :]
    item: dict[str, Any] = {"name": name, "sdk": sdk, "container": {"image": image}}
    if sdk in {"java", "java-lite"}:
        item["build"] = _build(cell.flavor, lite=sdk == "java-lite")
        if cell.flavor == "jvm":
            item["jvm"] = {"args": ["-XX:+UseG1GC"]}
    elif cell.flavor != "default":
        raise ValueError(f"Unsupported release recipe flavor for {image}")
    return field, item


def _entries(value: object) -> dict[tuple[str, str], dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("Release recipe components must be lists")
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in value:
        entry = _object(raw, "release component")
        name, sdk = entry.get("name"), entry.get("sdk")
        if (
            not isinstance(name, str)
            or not isinstance(sdk, str)
            or (name, sdk) in entries
        ):
            raise ValueError("Invalid or duplicate release recipe component")
        entries[(name, sdk)] = entry
    return entries


def _check_profile(
    profile: Mapping[str, Any],
    *,
    name: str,
    flavor: ImageFlavor,
    cells: tuple[ImageCell, ...],
    modules: tuple[str, ...],
) -> None:
    if (
        set(profile)
        - {"schemaVersion", "name", "registry", "controlPlane", "functions", "services"}
        or profile.get("schemaVersion") != 2
        or profile.get("name") != name
    ):
        raise ValueError("Unsupported release recipe schema or name")
    registry = _object(profile.get("registry"), "release registry")
    tag = registry.get("tag")
    if (
        set(registry) != {"repository", "tag"}
        or registry.get("repository") != DEFAULT_REGISTRY
        or not isinstance(tag, str)
        or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag) is None
    ):
        raise ValueError("Unsupported release recipe registry/tag")
    control = _object(profile.get("controlPlane"), "release controlPlane")
    expected = _control(flavor, modules)
    declared = control.get("modules")
    if (
        not isinstance(declared, list)
        or not all(isinstance(m, str) for m in declared)
        or sorted(declared) != list(modules)
    ):
        raise ValueError("Release recipe modules differ from guarded all selection")
    if {**control, "modules": list(modules)} != expected:
        raise ValueError("Release recipe control plane differs from release policy")
    components: dict[str, list[dict[str, Any]]] = {"functions": [], "services": []}
    for cell in cells:
        if cell.target.name != "control-plane":
            field, item = _component(cell)
            components[field].append(item)
    for field, entries in components.items():
        if _entries(profile.get(field, [])) != _entries(entries):
            raise ValueError(f"Release recipe {field} differ from guarded image matrix")
    if (flavor != "default") != any(
        cell.target.name == "control-plane" for cell in cells
    ):
        raise ValueError("Release control plane image missing from matrix")


def prepare_release_recipe_groups(
    source_tree: Path,
    image_plan: ImagePlan,
    *,
    profiles_root: Path,
    architecture: ImageArchitecture = "amd64",
) -> tuple[ReleaseRecipeGroup, ...]:
    """Freeze profiles only after exact guarded matrix and policy validation."""
    if (
        architecture not in {"amd64", "arm64"}
        or image_plan.registry != DEFAULT_REGISTRY
        or not image_plan.cells
        or any(cell.architecture != architecture for cell in image_plan.cells)
    ):
        raise ValueError("Release recipes require a matching local architecture matrix")
    if len({cell.image for cell in image_plan.cells}) != len(image_plan.cells):
        raise ValueError("Duplicate release image matrix cells")
    modules = release_modules(source_tree)
    groups: list[ReleaseRecipeGroup] = []
    for flavor in ("jvm", "native", "default"):
        cells = tuple(cell for cell in image_plan.cells if cell.flavor == flavor)
        if not cells:
            raise ValueError(f"Empty release recipe group {flavor}")
        name = f"release-{architecture}-{flavor}"
        try:
            raw = (profiles_root / f"{name}.yaml").read_bytes()
        except OSError as error:
            raise ValueError(f"Cannot read release recipe profile {name}") from error
        profile = _object(yaml.load(raw, Loader=_ProfileLoader), "release profile")  # nosec B506 - SafeLoader subclass rejects duplicate keys
        _check_profile(profile, name=name, flavor=flavor, cells=cells, modules=modules)
        tag = (
            image_plan.version
            + "-"
            + architecture
            + ("" if flavor == "default" else "-" + flavor)
        )
        if any(
            cell.tag != tag
            or cell.image != f"{image_plan.registry}/{cell.target.name}:{tag}"
            for cell in cells
        ):
            raise ValueError("Release recipe cells have mismatched image tags")
        groups.append(
            ReleaseRecipeGroup(
                flavor,
                name,
                raw,
                "sha256:" + hashlib.sha256(raw).hexdigest(),
                tag,
                cells,
                modules,
            )
        )
    if sum(len(group.cells) for group in groups) != len(image_plan.cells):
        raise ValueError("Release recipes do not cover the complete image matrix")
    return tuple(groups)


def _report_metadata(kind: str, item: Mapping[str, Any]) -> dict[str, Any]:
    name, sdk = item["name"], item["sdk"]
    build = item.get("build", {})
    mode = build.get("mode", "container")
    artifact = None
    if kind == "control-plane":
        artifact = "control-plane/"
    elif mode != "container":
        field = "functions" if kind == "function" else "services"
        artifact = f"{field}/{sdk}/{name}/"
    result = {
        "kind": kind,
        "name": name,
        "sdk": sdk,
        "mode": mode,
        "artifact": artifact,
    }
    if kind == "control-plane":
        result.update(
            variant=build["variant"], optimization="3" if mode == "native" else "c2"
        )
    if mode == "native":
        lite = sdk == "java-lite"
        result["native"] = {
            "optimization": "3",
            "gc": "serial" if lite else "G1",
            "monitoring": [] if lite else ["jfr"],
            "builder": "container",
            "distribution": "community" if lite else "oracle",
        }
    return result


def read_release_distribution(
    report: Path, *, group: ReleaseRecipeGroup, source_commit: str | None = None
) -> tuple[RecipeComponent, ...]:
    """Require complete assembly identity, leaving archive proof to the caller."""
    try:
        data = _object(
            json.loads(
                report.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object
            ),
            "release report",
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read release recipe report {report}") from error
    if (
        set(data)
        != {"schemaVersion", "recipe", "tag", "source", "modules", "components"}
        or type(data.get("schemaVersion")) is not int
        or data["schemaVersion"] != 2
    ):
        raise ValueError("Unsupported release recipe report schema")
    identity = _object(data.get("recipe"), "release recipe identity")
    if (
        identity
        != {
            "name": group.name,
            "sha256": group.profile_digest.removeprefix("sha256:"),
            "schemaVersion": 2,
        }
        or type(identity.get("schemaVersion")) is not int
        or data.get("tag") != group.tag
    ):
        raise ValueError(
            "Release recipe report identity/tag differs from frozen inputs"
        )
    modules = data.get("modules")
    if (
        not isinstance(modules, list)
        or not all(isinstance(m, str) for m in modules)
        or sorted(modules) != list(group.modules)
    ):
        raise ValueError("Release recipe report modules differ from frozen inputs")
    source = data["source"]
    if source is not None:
        source = _object(source, "release source")
        if (
            source_commit is None
            or set(source) != {"revision", "dirty"}
            or source.get("revision") != source_commit
            or source.get("dirty") is not False
        ):
            raise ValueError("Release recipe report source differs from guarded commit")

    declared: list[tuple[str, dict[str, Any]]] = [
        (
            "control-plane",
            {
                "name": "control-plane",
                "sdk": "java",
                **_control(group.flavor, group.modules),
            },
        )
    ]
    for cell in group.cells:
        if cell.target.name != "control-plane":
            field, item = _component(cell)
            declared.append(("function" if field == "functions" else "service", item))
    expected = {
        (kind, item["name"], item["sdk"]): (item, _report_metadata(kind, item))
        for kind, item in declared
    }
    rows = data.get("components")
    if not isinstance(rows, list):
        raise ValueError("Release recipe report components must be a list")
    seen: set[tuple[str, str, str]] = set()
    components: list[RecipeComponent] = []
    for raw in rows:
        row = _object(raw, "release report component")
        kind, name, sdk = (
            _string(row.get(field), field) for field in ("kind", "name", "sdk")
        )
        key = (kind, name, sdk)
        if key in seen or key not in expected:
            raise ValueError("Duplicate or unexpected release recipe report component")
        seen.add(key)
        item, metadata = expected[key]
        if (
            "image" not in row
            or {k: v for k, v in row.items() if k != "image"} != metadata
        ):
            raise ValueError(
                "Release recipe report component metadata differs from policy"
            )
        if "container" not in item:
            if (
                key != ("control-plane", "control-plane", "java")
                or group.flavor != "default"
                or row["image"] is not None
            ):
                raise ValueError("Invalid artifact-only release recipe component")
            continue
        image = _object(row["image"], "release image")
        reference = f"{DEFAULT_REGISTRY}/{item['container']['image']}:{group.tag}"
        if (
            set(image) - {"reference", "status", "id", "digest"}
            or image.get("reference") != reference
            or image.get("status") != "built"
            or image.get("digest") is not None
        ):
            raise ValueError(
                "Release recipe report image differs from complete local assembly"
            )
        image_id = sha256_digest(image.get("id"))
        components.append(
            RecipeComponent(
                kind=key[0],
                name=key[1],
                sdk=key[2],
                mode=row["mode"],
                image=RecipeImage(reference, image_id, "built", None),
                variant=row.get("variant"),
                optimization=row.get("optimization"),
                native=row.get("native"),
            )
        )
    if (
        seen != set(expected)
        or {component.image.reference for component in components}
        != {cell.image for cell in group.cells}
        or len(components) != len(group.cells)
    ):
        raise ValueError("Release recipe report does not cover the full group matrix")
    return tuple(components)
