from types import SimpleNamespace

import pytest

from nanolab.config.one_shot import ArtifactReference, ProtocolSettings
from nanolab.one_shot.models import TimingQualification
from nanolab.tasks.one_shot.artifacts import canonical_bytes, content_hash
from nanolab.tasks.one_shot.experiment import read_qualification


def settings(tmp_path, *, broken):
    timing = TimingQualification(
        provider="multipass",
        purpose="workflow-validation",
        environment_fingerprint="fp",
        profile_sha256="a" * 64,
        qualified=True,
        period_seconds=20,
        lead_seconds=2,
        quantile=0.9,
        quantile_seconds=0.1,
        margin_seconds=0.2,
        epsilon=0.1,
        sample_count=10,
        censored_count=0,
        samples_seconds=[0.1] * 10,
        period_candidates=[20],
        max_trace_resolution=30,
    )
    cell = {"id": "balanced", "rates": {"edge-0": {"work": 4}, "edge-1": {"work": 4}}}
    timing.matrix = [{"cell": cell, "result": timing.model_dump()}]
    timing.protocol = ProtocolSettings().model_dump(by_alias=True)
    if broken == "protocol":
        timing.protocol.pop("maxPeers")
    elif broken == "scope":
        cell["rates"].pop("edge-1")
    elif broken == "nested":
        nested = timing.matrix[0]["result"]
        assert isinstance(nested, dict)
        nested["profile_sha256"] = "b" * 64
    path = tmp_path / "timing.json"
    content = canonical_bytes(timing.model_dump())
    path.write_bytes(content)
    return SimpleNamespace(
        qualification=ArtifactReference(path=path, sha256=content_hash(content)),
        profile=SimpleNamespace(sha256="a" * 64),
        provider="multipass",
        purpose="workflow-validation",
        functions={"work": object()},
        nodes=[
            SimpleNamespace(id="edge-0", kind="edge"),
            SimpleNamespace(id="edge-1", kind="edge"),
        ],
    )


@pytest.mark.parametrize("broken", ["protocol", "scope", "nested"])
def test_qualification_rejects_incomplete_or_mixed_scope_before_load(tmp_path, broken):
    with pytest.raises(ValueError, match="timing"):
        read_qualification(settings(tmp_path, broken=broken))


def test_qualified_scope_is_accepted(tmp_path):
    assert read_qualification(settings(tmp_path, broken=None)).qualified
