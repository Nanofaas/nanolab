"""Verify the content identities used by classic and containerd Docker stores."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path


def archive_image_ids(
    path: Path, expected: dict[str, str], *, architecture: str | None = None
) -> dict[str, set[str]]:
    """Accept target IDs only when their manifests bind the expected config bytes."""
    identities = {reference: set() for reference in expected}
    with tarfile.open(path) as archive:

        def read(name: str) -> bytes:
            file = archive.extractfile(name)
            if file is None:
                raise ValueError("image archive blob is not a regular file")
            return file.read()

        def blob(digest: str) -> bytes:
            content = read("blobs/sha256/" + digest.removeprefix("sha256:"))
            if "sha256:" + hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("exported blob digest mismatch")
            return content

        def config_identity(content: bytes) -> str:
            if (
                architecture is not None
                and json.loads(content).get("architecture") != architecture
            ):
                raise ValueError("exported image architecture mismatch")
            return "sha256:" + hashlib.sha256(content).hexdigest()

        if "index.json" in archive.getnames():
            index = json.loads(read("index.json"))
            if index["schemaVersion"] != 2:
                raise ValueError("unsupported OCI image index")
            for descriptor in index["manifests"]:
                reference = descriptor.get("annotations", {}).get(
                    "io.containerd.image.name"
                )
                if reference not in expected:
                    continue
                digest = descriptor["digest"]
                manifest = json.loads(blob(digest))
                chain = {digest}
                if "manifests" in manifest:
                    # BuildKit attestations are not runnable platform images.
                    platforms = [
                        child
                        for child in manifest["manifests"]
                        if child.get("annotations", {}).get("vnd.docker.reference.type")
                        != "attestation-manifest"
                    ]
                    if len(platforms) != 1:
                        raise ValueError(
                            "image archive must contain one runtime platform"
                        )
                    digest = platforms[0]["digest"]
                    manifest = json.loads(blob(digest))
                    chain.add(digest)
                if manifest.get("schemaVersion") != 2 or "config" not in manifest:
                    raise ValueError("unsupported OCI runtime manifest")
                config = config_identity(blob(manifest["config"]["digest"]))
                chain.add(config)
                if expected[reference] not in chain:
                    raise ValueError("manifest does not bind expected image digest")
                identities[reference].update(chain)

        for entry in json.loads(read("manifest.json")):
            for reference in entry["RepoTags"] or []:
                if reference not in expected:
                    continue
                config = config_identity(read(entry["Config"]))
                if config not in identities[reference] | {expected[reference]}:
                    raise ValueError("exported image config digest mismatch")
                identities[reference].add(config)
    if any(not values for values in identities.values()):
        raise ValueError("exported archive is missing selected images")
    return identities
