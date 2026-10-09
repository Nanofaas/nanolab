"""Verify the content identities used by classic and containerd Docker stores."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path


def archive_image_ids(path: Path, expected: dict[str, str]) -> dict[str, set[str]]:
    """Accept target IDs only when their manifests bind the expected config bytes."""
    identities = {reference: set() for reference in expected}
    with tarfile.open(path) as archive:

        def read(name: str) -> bytes:
            file = archive.extractfile(name)
            if file is None:
                raise ValueError("image archive blob is not a regular file")
            return file.read()

        # Named saves retain every image in both legacy and OCI importers.
        for entry in json.loads(read("manifest.json")):
            config_id = "sha256:" + hashlib.sha256(read(entry["Config"])).hexdigest()
            for reference in entry["RepoTags"] or []:
                if reference in expected:
                    if config_id != expected[reference]:
                        raise ValueError("exported image config digest mismatch")
                    identities[reference].add(config_id)
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
                body = read("blobs/sha256/" + digest.removeprefix("sha256:"))
                if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
                    raise ValueError("exported manifest digest mismatch")
                manifest = json.loads(body)
                if manifest["config"]["digest"] != expected[reference]:
                    raise ValueError("manifest does not bind expected config digest")
                identities[reference].add(digest)
    if any(not values for values in identities.values()):
        raise ValueError("exported archive is missing selected images")
    return identities
