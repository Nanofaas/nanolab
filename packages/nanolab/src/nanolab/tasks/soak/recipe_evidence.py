"""Offline verification of retained recipe publication and compiler evidence."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from nanolab.config.soak import SoakConfig
from nanolab.tasks.recipe_multiarch import read_buildx_distribution
from nanolab.tasks.recipe_registry import RegistryResponse
from nanolab.tasks.soak.artifacts import fingerprint
from nanolab.tasks.soak.build_provenance import (
    BuildProvenanceCollector,
    _decode,
    _materials,
    _predicate,
    _read,
)
from nanolab.tasks.soak.images import BuildRecipe
from nanolab.tasks.soak.recipe import validate_soak_recipe
from nanolab.tasks.soak.recipe_observation import _build_inputs
from nanolab.tasks.soak.recipe_registry import verify_soak_registry
from nanolab.tasks.soak.sources import SourceSnapshot


def verify_soak_recipe_receipt(
    root: Path, config: SoakConfig, source: SourceSnapshot, build: dict[str, Any]
) -> None:
    """Recheck retained artifacts and command observations without Docker."""
    root = root.absolute()
    sealed = set()
    for descriptor in build["logs"]:
        path = Path(descriptor["path"])
        if not path.is_absolute():
            path = root / path
        body = _read(path, root)
        if descriptor.get("sha256") != hashlib.sha256(
            body
        ).hexdigest() or descriptor.get("size_bytes") != len(body):
            raise ValueError("Recipe evidence checksum differs from frozen receipt")
        sealed.add(path)

    def read(path: Path) -> bytes:
        if path not in sealed:
            raise ValueError("Recipe acceptance input is absent from frozen receipt")
        return _read(path, root)

    role = build["role"]
    base = root / "builds/recipe"
    observer = base / "observer"
    profile = base / "recipe.yaml"
    profile_hash = hashlib.sha256(read(profile)).hexdigest()
    validate_soak_recipe(profile, config, platform=build["platform"])
    identity = _decode(read(base / "source-identity.json"))
    if (
        any(
            identity.get(key) != value
            for key, value in {
                "revision": source.revision,
                "dirty": source.dirty,
                "fingerprint": source.fingerprint,
                "recipe_sha256": profile_hash,
            }.items()
        )
        or build.get("original_recipe_fingerprint") != profile_hash
    ):
        raise ValueError("Recipe acceptance source/profile identity differs")
    report = base / "distribution/distribution.json"
    data = _decode(read(report))
    if data.get("source", {}).get("revision") != identity.get("staging_revision"):
        raise ValueError("Recipe staging revision differs from publication")
    distribution = read_buildx_distribution(
        report,
        recipe=profile,
        tag=identity["tag"],
        expected_source=data["source"],
        platforms=frozenset({build["platform"]}),
        provenance=True,
    )
    component = next(
        item
        for item in distribution.components
        if (
            "control-plane"
            if item.kind == "control-plane"
            else f"{item.name}-{item.sdk}"
        )
        == role
    )
    repository, tag = component.image.reference.removeprefix("127.0.0.1:5000/").rsplit(
        ":", 1
    )
    raw = root / "builds/registry" / role
    responses = {}
    for name, kind in (
        ("index", "manifests"),
        ("manifest", "manifests"),
        ("attestation", "manifests"),
        ("config", "blobs"),
        ("statement-0", "blobs"),
    ):
        body = read(raw / (name + ".json"))
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        responses[kind, digest] = RegistryResponse(
            body, digest, _decode(body).get("mediaType", "")
        )
    tags = [read(raw / "tag-before.json"), read(raw / "tag-after.json")]

    def fetch(repo: str, kind: str, reference: str) -> RegistryResponse:
        if repo != repository:
            raise ValueError("Recipe registry repository differs from publication")
        if reference == tag:
            body = tags.pop(0)
            return RegistryResponse(
                body, "sha256:" + hashlib.sha256(body).hexdigest(), ""
            )
        return responses[kind, reference]

    image = verify_soak_registry(
        replace(distribution, components=(component,)),
        platform=build["platform"],
        evidence_dir=None,
        fetch=fetch,
    )[role]
    if build["image_digest"] != f"127.0.0.1:5000/{repository}@{image.manifest_digest}":
        raise ValueError("Recipe executable digest differs from frozen receipt")
    facts = _decode(read(observer / (role + "-observations.json")))
    request = _decode(read(observer / "publication-request.json"))
    if (
        facts.get("request_id") != request.get("request_id")
        or facts.get("source_fingerprint") != source.fingerprint
        or facts.get("profile_sha256") != profile_hash
        or facts.get("effective_recipe_fingerprint") != build["recipe_fingerprint"]
        or facts.get("image_digest") != image.publication_digest
    ):
        raise ValueError("Recipe compiler observation is unbound")
    publication = facts["publication"]
    metadata = _decode(read(observer / publication["metadata"]["path"]))
    if (
        metadata.get("containerimage.digest") != image.publication_digest
        or metadata.get("image.name") != component.image.reference
    ):
        raise ValueError("Recipe build metadata differs from registry")
    bases = _materials(
        _predicate(image.provenance[0], build["platform"], image.manifest_digest)
    )
    if bases != _materials(
        _predicate(
            metadata["buildx.build.provenance"],
            build["platform"],
            image.manifest_digest,
        )
    ):
        raise ValueError("Recipe material observations differ from registry")
    recipe = BuildRecipe(**facts["recipe"])
    command = next(item for item in facts["commands"] if item["kind"] == "build")
    if (
        recipe.role != role
        or recipe.platform != build["platform"]
        or recipe.variant != config.images[role].variant
        or recipe.image != component.image.reference
        or recipe.recipe_fingerprint != build["recipe_fingerprint"]
        or request.get("source_fingerprint") != source.fingerprint
        or request.get("profile_sha256") != profile_hash
        or publication.get("exit_code") != 0
        or publication.get("workspace") != request.get("workspace")
        or _build_inputs(command["argv"]) != _build_inputs(publication["actual"])
        or fingerprint(
            {
                "profile": profile_hash,
                "source": source.fingerprint,
                "command": command["argv"],
                "publication_command": publication["actual"],
                "instrumentation": request["instrumentation"],
            }
        )
        != recipe.recipe_fingerprint
    ):
        raise ValueError("Recipe effective inputs differ from frozen receipt")
    tools, _ = BuildProvenanceCollector(lambda *_args: b"")._commands(  # noqa: SLF001
        {"commands": facts["commands"]},
        {
            "workspace": request["workspace"],
            "build_argv": command["argv"],
            "prerequisite_argv": list(recipe.prerequisite_argv or ()),
        },
        recipe,
        metadata["buildx.build.ref"],
        bases,
        base,
    )
    if tools != dict(build["toolchains"]) or bases != dict(build["base_images"]):
        raise ValueError("Recipe effective toolchains/materials differ from receipt")
