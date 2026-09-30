"""Attested recipe artifacts are verified before their digests enter a soak."""

import hashlib
import json

import pytest
import yaml

from nanolab.tasks.recipe_multiarch import (
    MultiarchComponent,
    MultiarchDistribution,
    MultiarchImage,
)
from nanolab.tasks.recipe_registry import RegistryResponse

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
STATEMENT = "application/vnd.in-toto+json"


def artifact_fixture(tmp_path, mutation=None):
    blobs = {}

    def blob(kind, data):
        raw = json.dumps(data, separators=(",", ":")).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        blobs[kind, digest] = raw
        return digest, len(raw)

    config = {"os": "linux", "architecture": "arm64"}
    if mutation == "config-platform":
        config["architecture"] = "amd64"
    if mutation == "config-variant":
        config["variant"] = "v9"
    config_digest, config_size = blob("blobs", config)
    executable, executable_size = blob(
        "manifests",
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "config": {
                "mediaType": CONFIG,
                "digest": config_digest,
                "size": None if mutation == "config-size" else config_size,
            },
            "layers": [],
        },
    )
    predicate = {
        "buildType": "https://mobyproject.org/buildkit@v1",
        "buildConfig": {"llbDefinition": [{"id": "step0"}]},
        "metadata": {"completeness": {"parameters": True, "environment": True}},
        "materials": [
            {
                "uri": "pkg:docker/node@20-alpine?platform=linux%2Farm64",
                "digest": {"sha256": "a" * 64},
            }
        ],
    }
    if mutation == "missing-materials":
        predicate["materials"] = []
    if mutation == "minimum-provenance":
        predicate.pop("buildConfig")
    if mutation == "unsupported-predicate":
        predicate["buildType"] = "unknown"
    statement = {
        "_type": "https://in-toto.io/Statement/v0.1",
        "subject": [
            {
                "name": "image",
                "digest": {
                    "sha256": (
                        "b" * 64
                        if mutation == "wrong-subject"
                        else executable.removeprefix("sha256:")
                    )
                },
            }
        ],
        "predicateType": "https://slsa.dev/provenance/v0.2",
        "predicate": predicate,
    }
    if mutation == "unbound-provenance":
        statement = {"SLSA": predicate}
    statement_digest, statement_size = blob("blobs", statement)
    attestation, attestation_size = blob(
        "manifests",
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "config": {
                "mediaType": CONFIG,
                "digest": config_digest,
                "size": config_size,
            },
            "layers": [
                {
                    "mediaType": STATEMENT,
                    "digest": statement_digest,
                    "size": False if mutation == "statement-size" else statement_size,
                }
            ],
        },
    )
    executable_descriptor = {
        "mediaType": "unknown" if mutation == "manifest-media-type" else MANIFEST,
        "digest": executable,
        "size": -1 if mutation == "manifest-size" else executable_size,
        "platform": {"os": "linux", "architecture": "arm64"},
    }
    attestation_descriptor = {
        "mediaType": MANIFEST,
        "digest": attestation,
        "size": attestation_size,
        "platform": {"os": "unknown", "architecture": "unknown"},
        "annotations": {
            "vnd.docker.reference.type": "attestation-manifest",
            "vnd.docker.reference.digest": (
                "sha256:" + "b" * 64
                if mutation == "foreign-attestation"
                else executable
            ),
        },
    }
    descriptors = [executable_descriptor, attestation_descriptor]
    if mutation == "duplicate-executable":
        descriptors.append(executable_descriptor)
    if mutation == "missing-attestation":
        descriptors.pop()
    index, _ = blob(
        "manifests", {"schemaVersion": 2, "mediaType": INDEX, "manifests": descriptors}
    )
    if mutation == "wrong-hash":
        blobs["manifests", index] += b" "
    if mutation == "duplicate-json":
        raw = blobs["manifests", index].replace(
            b'"schemaVersion":2', b'"schemaVersion":2,"schemaVersion":2'
        )
        index = "sha256:" + hashlib.sha256(raw).hexdigest()
        blobs["manifests", index] = raw
    blobs["manifests", "run-1"] = blobs["manifests", index]
    image = MultiarchImage(
        "127.0.0.1:5000/nanofaas/control-plane:run-1",
        "published",
        index,
        ("linux/arm64",),
        {"linux/arm64": executable},
        True,
    )
    distribution = MultiarchDistribution(
        tmp_path / "report.json",
        "a" * 64,
        "run-1",
        {"revision": "source", "dirty": False},
        ("build-metadata",),
        (
            MultiarchComponent(
                "control-plane", "control-plane", "java", "jvm", image, "jvm", "default"
            ),
        ),
    )
    tag_reads = 0

    def fetch(repository, kind, reference):
        nonlocal tag_reads
        assert repository == "nanofaas/control-plane"
        raw = blobs[kind, reference]
        if reference == "run-1":
            tag_reads += 1
            if mutation == "changed-tag" and tag_reads == 2:
                raw += b" "
        return RegistryResponse(
            raw, "sha256:" + hashlib.sha256(raw).hexdigest(), "application/json"
        )

    return distribution, fetch, config_digest


def test_attested_publication_selects_only_executable_manifest(tmp_path):
    from nanolab.tasks.soak.recipe_registry import verify_soak_registry

    distribution, fetch, config_digest = artifact_fixture(tmp_path)
    result = verify_soak_registry(
        distribution,
        platform="linux/arm64",
        evidence_dir=tmp_path / "evidence",
        fetch=fetch,
    )
    image = result["control-plane"]
    assert image.publication_digest == distribution.components[0].image.digest
    assert (
        image.manifest_digest
        == distribution.components[0].image.manifests["linux/arm64"]
    )
    assert image.config_digest == config_digest
    assert image.manifest_digest != image.publication_digest
    assert len(image.provenance) == 1
    assert any(p.name == "statement-0.json" for p in (tmp_path / "evidence").rglob("*"))


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-hash",
        "duplicate-json",
        "config-platform",
        "config-variant",
        "config-size",
        "manifest-media-type",
        "manifest-size",
        "statement-size",
        "duplicate-executable",
        "missing-attestation",
        "foreign-attestation",
        "wrong-subject",
        "missing-materials",
        "minimum-provenance",
        "unsupported-predicate",
        "changed-tag",
        "unbound-provenance",
    ],
)
def test_invalid_publication_fails_before_runtime(tmp_path, mutation):
    from nanolab.tasks.soak.recipe_registry import verify_soak_registry

    distribution, fetch, _ = artifact_fixture(tmp_path, mutation)
    with pytest.raises(
        ValueError,
        match=r"Registry|registry|provenance|material|subject|Duplicate|descriptor",
    ):
        verify_soak_registry(
            distribution,
            platform="linux/arm64",
            evidence_dir=tmp_path / "evidence",
            fetch=fetch,
        )


def test_buildx_reader_supports_one_attested_platform_without_local_id(tmp_path):
    from nanolab.tasks.recipe_multiarch import read_buildx_distribution
    from tests.tasks.test_recipe_multiarch import SOURCE, multiarch_fixture

    recipe, report, data = multiarch_fixture(tmp_path)
    profile = yaml.safe_load(recipe.read_text())
    profile["registry"]["platforms"] = ["linux/arm64"]
    profile["registry"]["provenance"] = True
    recipe.write_text(yaml.safe_dump(profile))
    data["recipe"]["sha256"] = hashlib.sha256(recipe.read_bytes()).hexdigest()
    for component in data["components"]:
        image = component["image"]
        image["platforms"] = ["linux/arm64"]
        image["manifests"] = {"linux/arm64": image["manifests"]["linux/arm64"]}
        image["provenance"] = True
    report.write_text(json.dumps(data))
    result = read_buildx_distribution(
        report,
        recipe=recipe,
        tag="run-1",
        expected_source=SOURCE,
        platforms=frozenset({"linux/arm64"}),
        provenance=True,
    )
    assert len(result.components) == 2
    assert all(
        component.image.platforms == ("linux/arm64",) for component in result.components
    )
