import json
from types import SimpleNamespace

import pytest

from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot.contracts import contract_asset
from nanolab.one_shot.models import CalibrationProfile
from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash
from nanolab.tasks.one_shot.qualification import verify_profile_scope


@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "image",
        "input",
        "cpu",
        "coLocation",
        "replicas",
        "fingerprint",
        "function",
    ],
)
def test_profile_reuse_requires_identical_measured_scope(monkeypatch, mismatch):
    doc = json.loads(contract_asset("examples/synthetic-profile.json").read_bytes())
    doc.update(synthetic=False, provider="multipass")
    doc["functions"] = [doc["functions"][0]]
    row = doc["functions"][0]
    row.update(
        function="one-shot-workload",
        inputHash="sha256:" + content_hash(canonical_bytes({})),
        coLocation=[],
        cpuQuota=1,
        memoryMiB=128,
        backend="container-local",
        runtime="HTTP",
    )
    row["validity"].update(minReplicas=1, maxReplicas=2)
    settings = OneShotConfig.model_validate(
        {
            "provider": "multipass",
            "purpose": "workflow-validation",
            "nodes": [
                {"id": "edge-0", "kind": "edge"},
                {"id": "edge-1", "kind": "edge"},
                {"id": "cloud", "kind": "cloud"},
            ],
            "functions": {"one-shot-workload": {"input": {}}},
        }
    )
    topology = SimpleNamespace(function_settings=settings.functions)
    built = SimpleNamespace(
        function=lambda *_: SimpleNamespace(
            image=SimpleNamespace(id=row["imageDigest"])
        )
    )
    image = row["imageDigest"]
    built.function = lambda *_: SimpleNamespace(image=SimpleNamespace(id=image))
    fingerprint = doc["environmentFingerprint"]
    if mismatch == "image":
        row["imageDigest"] = "sha256:" + "c" * 64
    if mismatch == "input":
        row["inputHash"] = "sha256:" + "b" * 64
    if mismatch == "cpu":
        row["cpuQuota"] = 2
    if mismatch == "coLocation":
        row["coLocation"] = ["other"]
    if mismatch == "replicas":
        row["validity"]["maxReplicas"] = 1
    if mismatch == "fingerprint":
        fingerprint = "other"
    if mismatch == "function":
        row["function"] = "other"
    profile = CalibrationProfile.model_validate(doc)
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.qualification.environment_identity",
        lambda *_: (fingerprint, {}),
    )
    if mismatch is not None:
        with pytest.raises(ValueError, match="mismatch"):
            verify_profile_scope(profile, settings, topology, None, built)
    else:
        assert (
            verify_profile_scope(profile, settings, topology, None, built)
            == fingerprint
        )
