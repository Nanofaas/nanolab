"""Compatibility checks for the pinned NanoFaaS one-shot contracts."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from nanolab.one_shot.contracts import contract_asset, validate_contract
from nanolab.one_shot.models import CalibrationProfile, verify_calibration


def measured_profile() -> dict:
    """Turn the frozen example into an explicitly measured local fixture."""
    data = json.loads(contract_asset("examples/synthetic-profile.json").read_text())
    data.update(provider="multipass", synthetic=False)
    return data


def test_frozen_profile_example_and_invalid_variants() -> None:
    """The shipped schema accepts its example and rejects incompatible data."""
    data = measured_profile()
    validate_contract("service-profile", data)
    for field, value in (("schemaVersion", 2), ("surprise", True)):
        invalid = deepcopy(data)
        invalid[field] = value
        with pytest.raises(ValueError, match=r"invalid|mismatch"):
            validate_contract("service-profile", invalid)
    invalid = deepcopy(data)
    invalid["functions"][0]["measurement"]["includesColdStarts"] = True
    with pytest.raises(ValueError, match=r"invalid|mismatch"):
        validate_contract("service-profile", invalid)


@pytest.mark.parametrize(
    ("provider", "fingerprint", "purpose"),
    [
        ("azure", "sha256:" + "a" * 64, "workflow-validation"),
        ("multipass", "sha256:" + "b" * 64, "workflow-validation"),
        ("multipass", "sha256:" + "a" * 64, "scientific-experiment"),
    ],
)
def test_profile_cannot_move_between_environments_or_purposes(
    provider: str, fingerprint: str, purpose: str
) -> None:
    """Local measurement cannot silently qualify another deployment."""
    profile = CalibrationProfile.model_validate(measured_profile())
    with pytest.raises(ValueError, match=r"invalid|mismatch"):
        verify_calibration(
            profile, provider=provider, fingerprint=fingerprint, purpose=purpose
        )


def test_same_measured_environment_is_accepted_and_synthetic_is_rejected() -> None:
    """Measured profiles are usable only in their recorded environment."""
    data = measured_profile()
    profile = CalibrationProfile.model_validate(data)
    verify_calibration(
        profile,
        provider="multipass",
        fingerprint=data["environmentFingerprint"],
        purpose="workflow-validation",
    )
    data["synthetic"] = True
    with pytest.raises(ValueError, match="synthetic"):
        verify_calibration(
            CalibrationProfile.model_validate(data),
            provider="multipass",
            fingerprint=data["environmentFingerprint"],
            purpose="workflow-validation",
        )


def test_prerequisite_bytes_must_match_frozen_hash(tmp_path) -> None:
    from nanolab.config.one_shot import ArtifactReference

    path = tmp_path / "profile.json"
    path.write_bytes(b"changed")
    reference = ArtifactReference(path=path, sha256="a" * 64)
    with pytest.raises(ValueError, match="hash"):
        reference.read_verified()
