"""Immutable per-node native profiles; legacy normalization requires homogeneity."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from nanolab.config.one_shot import (
    ArtifactReference,
    OneShotConfig,
    resolve_node_functions,
)
from nanolab.one_shot.models import CalibrationProfile, Purpose, verify_calibration


class ProfileSet(BaseModel):
    """NanoLab index binds node identity, native profile hashes and capacity bytes."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = Field(alias="schemaVersion")
    artifact_type: Literal["node-profile-set"] = Field(alias="artifactType")
    provider: Literal["multipass"]
    purpose: Purpose
    environment_fingerprint: str = Field(
        alias="environmentFingerprint", pattern=r"^sha256:[0-9a-f]{64}$"
    )
    nodes: dict[str, ArtifactReference]
    node_fingerprints: dict[str, str] = Field(alias="nodeFingerprints")
    capacity: ArtifactReference


@dataclass(frozen=True)
class CalibrationProfiles:
    """Local demands and measured capacity, never a fabricated global average."""

    by_node: dict[str, CalibrationProfile]
    sha256: str
    environment_fingerprint: str
    capacities: dict[str, float] | None = None


def relative_reference(root: Path, reference: ArtifactReference) -> ArtifactReference:
    """Forbid absolute paths, traversal and symlink escapes from a dossier."""
    if reference.path.is_absolute():
        raise ValueError("profile-set references must be relative")
    target = (root / reference.path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("profile reference escapes dossier")
    return reference.model_copy(update={"path": target})


def verify_node_resources(
    profile: CalibrationProfile, settings: OneShotConfig, node_id: str
) -> None:
    """Reject a quota, input or replica-range swap before provider acquisition."""
    from nanolab.functions.catalog import resolve_function_definition
    from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash
    from nanolab.workspace.paths import default_tool_paths

    measured = {
        str(row["function"]): cast(dict[str, Any], row) for row in profile.functions
    }
    resolved = resolve_node_functions(settings, node_id)
    names = {}
    for key in resolved:
        names[key] = (
            key
            if key in measured
            else resolve_function_definition(
                key, default_tool_paths().nanofaas_root
            ).family
        )
    if len(measured) != len(profile.functions) or set(names.values()) != set(measured):
        raise ValueError("profile function scope mismatch")
    for key, function in resolved.items():
        row = measured[names[key]]
        if (
            row["cpuQuota"] != function.cpu
            or row["memoryMiB"] != function.memory_mib
            or row["validity"]["minReplicas"] != 1
            or row["validity"]["maxReplicas"] < function.max_replicas
            or row["inputHash"]
            != "sha256:" + content_hash(canonical_bytes(function.input))
        ):
            raise ValueError("profile resource/input scope mismatch")


def read_profiles(settings: OneShotConfig) -> CalibrationProfiles:
    """Verify outer and nested bytes and match each node's measured environment."""
    if settings.profile is None:
        raise ValueError("independent measured profile is required")
    raw = json.loads(settings.profile.read_verified())
    expected = {node.id for node in settings.nodes}
    if raw.get("artifactType") != "node-profile-set":
        effective = [
            resolve_node_functions(settings, node.id) for node in settings.nodes
        ]
        if any(functions != effective[0] for functions in effective[1:]):
            raise ValueError("single profile requires homogeneous resource limits")
        profile = CalibrationProfile.model_validate(raw)
        verify_calibration(
            profile,
            provider=settings.provider,
            purpose=settings.purpose,
            fingerprint=profile.environment_fingerprint,
        )
        for node in expected:
            verify_node_resources(profile, settings, node)
        return CalibrationProfiles(
            dict.fromkeys(expected, profile),
            settings.profile.sha256,
            profile.environment_fingerprint,
        )
    index = ProfileSet.model_validate(raw)
    if index.provider != settings.provider or index.purpose != settings.purpose:
        raise ValueError("profile-set provider/purpose scope mismatch")
    if set(index.nodes) != expected or set(index.node_fingerprints) != expected:
        raise ValueError("profile-set node scope mismatch")
    root = settings.profile.path.parent
    profiles = {}
    for node, reference in index.nodes.items():
        profile = CalibrationProfile.model_validate_json(
            relative_reference(root, reference).read_verified()
        )
        verify_calibration(
            profile,
            provider=settings.provider,
            purpose=settings.purpose,
            fingerprint=index.node_fingerprints[node],
        )
        verify_node_resources(profile, settings, node)
        profiles[node] = profile
    capacity = json.loads(relative_reference(root, index.capacity).read_verified())
    if (
        capacity.get("schemaVersion") != 1
        or capacity.get("artifactType") != "measured-node-capacity"
        or capacity.get("environmentFingerprint") != index.environment_fingerprint
        or set(capacity.get("capacities", {})) != expected
    ):
        raise ValueError("measured capacity scope/fingerprint mismatch")
    values = capacity["capacities"]
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        for value in values.values()
    ):
        raise ValueError("invalid measured capacity scope")
    return CalibrationProfiles(
        profiles, settings.profile.sha256, index.environment_fingerprint, values
    )
