"""Versioned, immutable experiment inputs and atomic publication receipts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nanolab.cli.vm_provider import vm_request_for_role
from nanolab.comparison.matrix import ComparisonCell
from nanolab.comparison.profiles import COMPARISON_SCHEDULER_STRATEGY, declared_options
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.functions import ResolvedFunction, resolve_function
from nanolab.tasks.vm.models import VmRequest


class _Identity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    nanofaas: dict[str, object]
    nanolab: dict[str, object]
    profiles: dict[str, dict[str, object]]
    tag: str
    variants: list[str]
    repetitions: int
    nativeProperties: dict[str, object]  # noqa: N815 - manifest JSON contract
    scenario: dict[str, object]
    environment: dict[str, object]
    roles: dict[str, dict[str, object]]
    functions: dict[str, dict[str, object]]
    scheduler: dict[str, object]

    @model_validator(mode="after")
    def complete_identity(self) -> _Identity:
        for source in (self.nanofaas, self.nanolab):
            if not source.get("revision") or not _sha(source.get("patchSha256")):
                raise ValueError("incomplete Git identity")
        if self.scheduler != {
            "engine": "unified",
            "strategy": COMPARISON_SCHEDULER_STRATEGY,
            "runtimeSwitching": False,
        }:
            raise ValueError("missing or conflicting scheduler identity")
        if (
            not self.variants
            or len(set(self.variants)) != len(self.variants)
            or self.repetitions < 1
        ):
            raise ValueError("invalid matrix identity")
        for config in (self.scenario, self.environment):
            if not isinstance(config.get("values"), dict) or config.get(
                "sha256"
            ) != _hash(config["values"]):
                raise ValueError("incomplete configuration identity")
        for key in dict.fromkeys(("jvm", *self.variants)):
            profile = self.profiles.get(key, {})
            if not _sha(profile.get("sha256")) or not isinstance(
                profile.get("options"), dict
            ):
                raise ValueError("incomplete profile identity")
            options = profile["options"]
            assert isinstance(options, dict)  # nosec B101 - validated above
            if not {
                "mode",
                "variant",
                "modules",
                "jvm_args",
                "native",
                "functions",
            }.issubset(options):
                raise ValueError("incomplete profile options identity")
        required_function_fields = {field.name for field in fields(ResolvedFunction)}
        for function in self.functions.values():
            if not required_function_fields.issubset(function):
                raise ValueError("incomplete resolved function identity")
        required_request_fields = set(VmRequest.model_fields) - {"proxmox_password"}
        for request in self.roles.values():
            if not required_request_fields.issubset(request):
                raise ValueError("incomplete VM request identity")
        if not self.roles or not self.functions or not self.tag:
            raise ValueError("incomplete resolved inputs")
        return self


def _sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ("git", *args), cwd=root, check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"Cannot establish Git identity: {root}") from error


def _source_identity(root: Path) -> dict[str, object]:
    return {
        "revision": _git(root, "rev-parse", "HEAD").decode().strip(),
        "patchSha256": hashlib.sha256(
            _git(root, "diff", "HEAD", "--binary", "--no-ext-diff")
        ).hexdigest(),
    }


def capture_comparison_inputs(
    *,
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    nanofaas_root: Path,
    nanolab_root: Path,
    profiles: Mapping[str, Path],
    variants: tuple[str, ...],
    repetitions: int,
    tag: str,
    build_memory: str | None,
    parallelism: int | None,
) -> dict[str, object]:
    """Capture tracked Git state and complete effective experiment settings."""
    scenario_values = scenario.model_dump(mode="json")
    environment_values = environment.model_dump(mode="json")
    roles = {
        role: vm_request_for_role(environment, role, loadtest=True).model_dump(
            mode="json", exclude={"proxmox_password"}
        )
        for role in ("stack", "loadgen")
    }
    tool_root = nanolab_root / "packages" / "nanolab"
    if not tool_root.is_dir():
        tool_root = nanolab_root
    inputs: dict[str, object] = {
        "nanofaas": _source_identity(nanofaas_root),
        "nanolab": _source_identity(nanolab_root),
        "profiles": {
            key: {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "options": declared_options(path),
            }
            for key, path in profiles.items()
        },
        "tag": tag,
        "variants": list(variants),
        "repetitions": repetitions,
        "nativeProperties": {"buildMemory": build_memory, "parallelism": parallelism},
        "scenario": {"values": scenario_values, "sha256": _hash(scenario_values)},
        "environment": {
            "values": environment_values,
            "sha256": _hash(environment_values),
        },
        "roles": roles,
        "functions": {
            key: asdict(
                resolve_function(
                    scenario, key, source_root=nanofaas_root, tool_root=tool_root
                )
            )
            for key in scenario.functions
        },
        "scheduler": {
            "engine": "unified",
            "strategy": COMPARISON_SCHEDULER_STRATEGY,
            "runtimeSwitching": False,
        },
    }
    # Normalize tuples in resolved functions to JSON's array representation.
    normalized = json.loads(json.dumps(inputs))
    _Identity.model_validate(normalized)
    return normalized


class ComparisonManifest(BaseModel):
    """An experiment's original inputs, infrastructure and verified artifacts."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Literal[1] = 1  # noqa: N815 - manifest JSON contract
    identity: dict[str, object]
    target: dict[str, object] | None = None
    publications: dict[str, dict[str, object]] = Field(default_factory=dict)
    started_at: str
    functions: list[str]
    repetitions: int
    order: list[str]
    variants: list[dict[str, object]]
    regime: dict[str, object]

    @model_validator(mode="after")
    def complete_manifest(self) -> ComparisonManifest:
        """Reject incomplete identities and publication receipts."""
        _Identity.model_validate(self.identity)
        for receipt in self.publications.values():
            for field in (
                "profile",
                "distribution",
                "evidence",
                "images",
                "declaredOptions",
                "source",
                "gradleProperties",
            ):
                if not isinstance(receipt.get(field), dict):
                    raise ValueError(f"incomplete publication receipt: {field}")
            for field in ("profile", "distribution"):
                descriptor = receipt[field]
                assert isinstance(descriptor, dict)  # nosec B101 - validated above
                if not isinstance(descriptor.get("path"), str) or not _sha(
                    descriptor.get("sha256")
                ):
                    raise ValueError(f"incomplete publication receipt: {field}")
            if not receipt["evidence"] or not receipt["images"]:
                raise ValueError("incomplete publication receipt: evidence/images")
            evidence = receipt["evidence"]
            assert isinstance(evidence, dict)  # nosec B101 - validated above
            for descriptor in evidence.values():
                if (
                    not isinstance(descriptor, dict)
                    or not isinstance(descriptor.get("path"), str)
                    or not _sha(descriptor.get("sha256"))
                ):
                    raise ValueError("incomplete publication receipt: evidence")
            images = receipt["images"]
            assert isinstance(images, dict)  # nosec B101 - validated above
            for image in images.values():
                if (
                    not isinstance(image, dict)
                    or not image.get("reference")
                    or not isinstance(image.get("digest"), str)
                    or not _sha(image["digest"].removeprefix("sha256:"))
                ):
                    raise ValueError("incomplete publication receipt: image")
            if receipt["source"] != self.identity["nanofaas"]:
                raise ValueError("incomplete publication receipt: source")
        return self


def read_comparison_manifest(root: Path) -> ComparisonManifest | None:
    """Read supported evidence, refusing nonempty roots without a valid manifest."""
    if not root.exists() or not any(root.iterdir()):
        return None
    try:
        return ComparisonManifest.model_validate_json(
            (root / "comparison-manifest.json").read_bytes()
        )
    except (OSError, ValueError) as error:
        raise ValueError(f"Invalid comparison manifest in {root}: {error}") from error


def require_matching_inputs(
    manifest: ComparisonManifest, inputs: Mapping[str, object]
) -> None:
    """Refuse changed inputs without modifying the original evidence."""

    def comparable(value: Mapping[str, object]) -> dict[str, object]:
        result = json.loads(json.dumps(dict(value)))
        for profile in result["profiles"].values():
            profile.pop("path", None)
        return result

    _Identity.model_validate(dict(inputs))
    if comparable(manifest.identity) != comparable(inputs):
        raise ValueError("Comparison inputs differ; use a new run directory")


def new_comparison_manifest(
    inputs: Mapping[str, object], cells: tuple[ComparisonCell, ...]
) -> ComparisonManifest:
    """Create initial evidence while retaining the report's existing fields."""
    identity = _Identity.model_validate(dict(inputs))
    variants = list(
        {
            cell.variant.key: {
                "key": cell.variant.key,
                "label": cell.variant.label,
                "rationale": cell.variant.rationale,
                "build_env": dict(cell.variant.build_env),
                "image": (
                    f"127.0.0.1:5000/nanofaas/control-plane-{cell.variant.key}"
                    f":{identity.tag}"
                ),
            }
            for cell in cells
        }.values()
    )
    return ComparisonManifest(
        identity=dict(inputs),
        started_at=datetime.now(UTC).isoformat(),
        functions=list(identity.functions),
        repetitions=max(cell.repetition for cell in cells),
        order=[cell.label for cell in cells],
        variants=variants,
        regime=identity.scenario,
    )


def write_comparison_manifest(root: Path, manifest: ComparisonManifest) -> None:
    """Atomically replace complete evidence using a same-directory temporary file."""
    validated = ComparisonManifest.model_validate(manifest.model_dump())
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=root, prefix=".comparison-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(validated.model_dump_json(indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.replace(root / "comparison-manifest.json")
    finally:
        temporary.unlink(missing_ok=True)
