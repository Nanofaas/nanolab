"""Resolve independent packaged-artifact oracles from one source snapshot."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from nanolab.comparison.prepare import captured_source_state
from nanolab.functions.catalog import list_functions
from nanolab.images.plan import ImageArchitecture, ImagePlan, build_image_plan
from nanolab.release.versioning import read_project_version

type JsonValue = (
    bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
)

CONTRACT_FAMILIES = ("word-stats", "json-transform", "roman-numeral", "qr-code")


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError(f"JSON numbers must be finite: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def strict_json(raw: bytes | str) -> JsonValue:
    """Decode JSON without silently losing duplicate keys or nonfinite values."""
    return cast(
        JsonValue,
        json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_nonfinite,
            parse_float=_finite_float,
        ),
    )


def same_json(left: JsonValue, right: JsonValue) -> bool:
    """Compare application JSON, preserving boolean identity and array order."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            same_json(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            same_json(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


@dataclass(frozen=True, slots=True)
class ContractCase:
    """A JSON expectation or a PNG oracle, independently specified by the corpus."""

    name: str
    input: JsonValue
    expected: JsonValue
    status: int
    png_size: int | None


@dataclass(frozen=True, slots=True)
class ContractMatrix:
    """All selected image cells and their frozen independent inputs."""

    images: ImagePlan
    cases: dict[str, tuple[ContractCase, ...]]
    source_state: dict[str, object]
    corpus_hashes: dict[str, str]
    subset: tuple[str, ...]


def _source_bytes(source: Path, path: Path) -> bytes:
    if not path.resolve().is_relative_to(source.resolve()) or not path.is_file():
        raise ValueError(f"contract input must be a file inside source: {path}")
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024)
    if len(raw) >= 2 * 1024 * 1024:
        raise ValueError(f"contract input exceeds byte bound: {path}")
    return raw


def _cases(raw: bytes, family: str) -> tuple[ContractCase, ...]:
    data = strict_json(raw)
    items = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError(f"missing corpus cases: {family}")
    result: list[ContractCase] = []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(name, str)
            or not name
            or "input" not in item
        ):
            raise ValueError(f"malformed corpus case: {family}")
        status = item.get("expectedStatusCode", 200)
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
        ):
            raise ValueError("invalid expected status")
        png_size = None
        if "expected" not in item:
            value = item["input"]
            if (
                family != "qr-code"
                or status != 200
                or not isinstance(value, dict)
                or not isinstance(value.get("text"), str)
                or not value["text"]
            ):
                raise ValueError(f"missing independent oracle: {item['name']}")
            size = value.get("size", 256)
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or not 128 <= size <= 1024
            ):
                raise ValueError("invalid PNG oracle dimensions")
            png_size = size
        case = ContractCase(name, item["input"], item.get("expected"), status, png_size)
        if any(
            case.name == other.name or same_json(case.input, other.input)
            for other in result
        ):
            raise ValueError("ambiguous corpus case")
        result.append(case)
    return tuple(result)


def resolve_contract_matrix(
    source: Path,
    selectors: tuple[str, ...],
    *,
    architecture: ImageArchitecture,
    tag: str,
) -> ContractMatrix:
    """Validate selection and payloads before any Docker resource acquisition."""
    source = source.resolve()
    if not selectors:
        raise ValueError("empty contract selection")
    if architecture not in {"amd64", "arm64"} or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,80}", tag
    ):
        raise ValueError("unsupported architecture or invalid attempt tag")
    catalog = list_functions(source)
    known = {
        function.key: function
        for function in catalog
        if function.example_dir is not None and function.family in CONTRACT_FAMILIES
    }
    selected: set[str] = set()
    for selector in selectors:
        if selector in CONTRACT_FAMILIES:
            selected.update(
                key for key, function in known.items() if function.family == selector
            )
        elif selector in known:
            selected.add(selector)
        else:
            raise ValueError(f"unknown or unsupported contract selector: {selector}")
    if not selected or any(
        selector in CONTRACT_FAMILIES
        and not any(known[key].family == selector for key in selected)
        for selector in selectors
    ):
        raise ValueError("empty contract selection for requested family")
    cases: dict[str, tuple[ContractCase, ...]] = {}
    hashes: dict[str, str] = {}
    targets: list[str] = []
    for key in sorted(selected):
        function = known[key]
        family = function.family
        if family not in cases:
            path = source / "functions/test-data" / family / "correctness.json"
            raw = _source_bytes(source, path)
            cases[family] = _cases(raw, family)
            hashes[str(path.relative_to(source))] = hashlib.sha256(raw).hexdigest()
        if function.example_dir is None:
            raise ValueError("contract implementation has no source")
        for payload in sorted((function.example_dir / "payloads").glob("*.json")):
            raw = _source_bytes(source, payload)
            value = strict_json(raw)
            if (
                not isinstance(value, dict)
                or "input" not in value
                or "expected" not in value
            ):
                raise ValueError(f"malformed wrapped payload: {payload}")
            matching = [
                case for case in cases[family] if same_json(case.input, value["input"])
            ]
            if (
                len(matching) != 1
                or matching[0].png_size is not None
                or not same_json(matching[0].expected, value["expected"])
            ):
                raise ValueError(f"payload contradicts independent oracle: {payload}")
            hashes[str(payload.relative_to(source))] = hashlib.sha256(raw).hexdigest()
        prefix = {"exec": "bash", "java-lite": "java-lite"}.get(
            function.runtime, function.runtime
        )
        targets.append(f"{prefix}-{family}")
    images = build_image_plan(
        source,
        read_project_version(source),
        registry=f"nanolab-contract/{tag}",
        selectors=targets,
        architectures=(architecture,),
    )
    return ContractMatrix(
        images, cases, captured_source_state(source), hashes, tuple(sorted(selected))
    )
