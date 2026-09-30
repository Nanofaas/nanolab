from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanolab.tasks.recipe_multiarch import read_multiarch_distribution
from tests.tasks.test_recipe_multiarch import SOURCE, multiarch_fixture


def registry_fixture(
    tmp_path: Path, *, wrong_config: bool = False
) -> tuple[Any, dict[tuple[str, str], bytes]]:
    recipe, report, data = multiarch_fixture(tmp_path)
    blobs: dict[tuple[str, str], bytes] = {}
    descriptors = []
    manifests = {}
    for architecture in ("amd64", "arm64"):
        config = json.dumps(
            {"architecture": "s390x" if wrong_config else architecture, "os": "linux"}
        ).encode()
        config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
        blobs["blobs", config_digest] = config
        manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": config_digest,
                    "size": len(config),
                },
                "layers": [],
            }
        ).encode()
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        blobs["manifests", digest] = manifest
        manifests[f"linux/{architecture}"] = digest
        descriptors.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": digest,
                "size": len(manifest),
                "platform": {"os": "linux", "architecture": architecture},
            }
        )
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": descriptors,
        }
    ).encode()
    index_digest = "sha256:" + hashlib.sha256(index).hexdigest()
    blobs["manifests", index_digest] = index
    blobs["manifests", "run-1"] = index
    for component in data["components"]:
        component["image"]["digest"] = index_digest
        component["image"]["manifests"] = manifests.copy()
    report.write_text(json.dumps(data))
    return read_multiarch_distribution(
        report, recipe=recipe, tag="run-1", expected_source=SOURCE
    ), blobs


def fixture_fetch(blobs: dict[tuple[str, str], bytes]):
    from nanolab.tasks.recipe_registry import RegistryResponse

    def fetch(repository: str, kind: str, reference: str) -> RegistryResponse:
        assert repository in {"nanofaas/control-plane", "nanofaas/word-stats-java"}
        body = blobs[kind, reference]
        return RegistryResponse(
            body, "sha256:" + hashlib.sha256(body).hexdigest(), "application/json"
        )

    return fetch


def test_verifies_both_platforms_and_retains_raw_bytes(tmp_path: Path) -> None:
    from nanolab.tasks.recipe_registry import verify_multiarch_registry

    distribution, blobs = registry_fixture(tmp_path)
    evidence = tmp_path / "registry"
    configs = verify_multiarch_registry(
        distribution, fetch=fixture_fetch(blobs), evidence_dir=evidence
    )
    assert len(configs) == 2
    for platforms in configs.values():
        assert set(platforms) == {"linux/amd64", "linux/arm64"}
        assert len(set(platforms.values())) == 2
    for component in distribution.components:
        stored = evidence / f"{component.kind}-{component.name}-{component.sdk}"
        assert (stored / "index.json").read_bytes() == blobs[
            "manifests", component.image.digest
        ]
        assert (stored / "linux-amd64-manifest.json").read_bytes() == blobs[
            "manifests", component.image.manifests["linux/amd64"]
        ]


@pytest.mark.parametrize(
    "failure",
    [
        "raw",
        "header",
        "missing",
        "moving-tag",
        "config-platform",
        "duplicate",
        "extra",
        "attestation",
        "wrong-child",
        "size",
    ],
)
def test_registry_identity_mismatch_stops_projection(
    tmp_path: Path, failure: str
) -> None:
    from nanolab.tasks.recipe_registry import (
        RegistryResponse,
        verify_multiarch_registry,
    )

    distribution, blobs = registry_fixture(tmp_path)
    if failure == "config-platform":
        distribution, blobs = registry_fixture(tmp_path, wrong_config=True)
    image = distribution.components[0].image
    if failure in {"duplicate", "extra", "attestation", "wrong-child", "size"}:
        index = json.loads(blobs["manifests", image.digest])
        descriptor = index["manifests"][0]
        if failure == "duplicate":
            index["manifests"].append(descriptor)
        elif failure == "extra":
            index["manifests"].append(
                {**descriptor, "platform": {"os": "linux", "architecture": "s390x"}}
            )
        elif failure == "attestation":
            descriptor["annotations"] = {
                "vnd.docker.reference.type": "attestation-manifest"
            }
        elif failure == "wrong-child":
            descriptor["digest"] = "sha256:" + "e" * 64
        else:
            descriptor["size"] += 1
        body = json.dumps(index).encode()
        new_digest = "sha256:" + hashlib.sha256(body).hexdigest()
        blobs["manifests", new_digest] = body
        blobs["manifests", "run-1"] = body
        distribution = replace(
            distribution,
            components=(
                replace(
                    distribution.components[0], image=replace(image, digest=new_digest)
                ),
                *distribution.components[1:],
            ),
        )
    calls = 0

    def fetch(repository: str, kind: str, reference: str) -> RegistryResponse:
        nonlocal calls
        if reference == "run-1":
            calls += 1
        if failure == "missing" and kind == "blobs":
            raise ValueError("Missing registry blob")
        response = fixture_fetch(blobs)(repository, kind, reference)
        if failure == "raw" and reference == image.digest:
            return replace(response, body=response.body + b" ")
        if failure == "header":
            return replace(response, digest="sha256:" + "e" * 64)
        if failure == "moving-tag" and reference == "run-1" and calls > 1:
            return replace(
                response,
                body=b"{}",
                digest="sha256:" + hashlib.sha256(b"{}").hexdigest(),
            )
        return response

    with pytest.raises(
        ValueError, match=r"digest|platform|manifest|size|blob|attestation"
    ):
        verify_multiarch_registry(
            distribution, fetch=fetch, evidence_dir=tmp_path / "evidence"
        )


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
def test_host_projection_uses_child_digest_and_config_id(
    tmp_path: Path, platform: str
) -> None:
    from nanolab.tasks.recipe_multiarch import project_host_distribution
    from nanolab.tasks.recipe_registry import verify_multiarch_registry

    distribution, blobs = registry_fixture(tmp_path)
    original = distribution.report.read_bytes()
    configs = verify_multiarch_registry(
        distribution, fetch=fixture_fetch(blobs), evidence_dir=tmp_path / "evidence"
    )
    runtime = project_host_distribution(
        distribution, platform=platform, configs=configs
    )
    for component, published in zip(
        runtime.components, distribution.components, strict=True
    ):
        child = published.image.manifests[platform]
        manifest = json.loads(blobs["manifests", child])
        assert (
            component.image.reference
            == published.image.reference.rsplit(":", 1)[0] + "@" + child
        )
        assert component.image.id == manifest["config"]["digest"]
        assert component.image.digest == child
        assert component.variant == published.variant
    assert distribution.report.read_bytes() == original


def test_registry_fetch_is_scoped_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from nanolab.tasks.recipe_registry import fetch_local_registry

    original_client = httpx.Client
    options = {}
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://unintended.invalid/"})

    def client(**kwargs):
        options.update(kwargs)
        return original_client(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "Client", client)
    with pytest.raises(ValueError, match="registry"):
        fetch_local_registry("nanofaas/control-plane", "manifests", "run-1")
    assert requests == [
        "http://127.0.0.1:5000/v2/nanofaas/control-plane/manifests/run-1"
    ]
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False
    assert options["timeout"] == 30
    for repository in [
        "../escape",
        "nanofaas/../../escape",
        "http://external.invalid/image",
    ]:
        with pytest.raises(ValueError, match="registry"):
            fetch_local_registry(repository, "manifests", "run-1")
    assert len(requests) == 1
