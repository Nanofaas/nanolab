"""Prove config and manifest digests denote the same exported image bytes."""

import hashlib
import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from nanolab.tasks.one_shot.images import archive_image_ids


def archive(tmp_path, *, corrupt=False):
    config = b'{"architecture":"arm64"}'
    config_id = "sha256:" + hashlib.sha256(config).hexdigest()
    manifest = json.dumps(
        {"schemaVersion": 2, "config": {"digest": config_id}, "layers": []}
    ).encode()
    manifest_id = "sha256:" + hashlib.sha256(manifest).hexdigest()
    entries = {
        "manifest.json": json.dumps(
            [
                {
                    "Config": "blobs/sha256/" + config_id[7:],
                    "RepoTags": ["fn:tag"],
                    "Layers": [],
                }
            ]
        ).encode(),
        "index.json": json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "digest": manifest_id,
                        "annotations": {"io.containerd.image.name": "fn:tag"},
                    }
                ],
            }
        ).encode(),
        "blobs/sha256/" + config_id[7:]: config,
        "blobs/sha256/" + manifest_id[7:]: b"changed" if corrupt else manifest,
    }
    path = tmp_path / "images.tar"
    with tarfile.open(path, "w") as output:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            output.addfile(info, io.BytesIO(content))
    component = SimpleNamespace(image=SimpleNamespace(id=config_id, reference="fn:tag"))
    return path, {"fn:tag": config_id}, config_id, manifest_id


def test_both_docker_image_stores_are_accepted_only_for_identical_bytes(tmp_path):
    path, components, config, manifest = archive(tmp_path)
    assert archive_image_ids(path, components) == {"fn:tag": {config, manifest}}


def test_corrupted_manifest_is_rejected_before_runtime_use(tmp_path):
    path, components, _, _ = archive(tmp_path, corrupt=True)
    with pytest.raises(ValueError, match="digest"):
        archive_image_ids(path, components)
