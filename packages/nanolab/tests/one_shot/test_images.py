"""Prove config and manifest digests denote the same exported image bytes."""

import hashlib
import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from nanolab.tasks.one_shot.images import archive_image_ids


def archive(tmp_path, *, corrupt=False, repo_tags=("fn:tag",)):
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
                    "RepoTags": repo_tags,
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


@pytest.mark.parametrize("repo_tags", [["fn:tag"], None, ["other:tag"]])
def test_archive_rejects_images_for_another_architecture(tmp_path, repo_tags):
    path, components, _, _ = archive(tmp_path, repo_tags=repo_tags)
    assert archive_image_ids(path, components, architecture="arm64")
    with pytest.raises(ValueError, match="architecture"):
        archive_image_ids(path, components, architecture="amd64")


@pytest.mark.parametrize("fault", [None, "digest", "architecture", "missing"])
def test_prebuilt_distribution_reuses_archive_without_docker(
    tmp_path, monkeypatch, fault
):
    from sonata_engine import TaskInputs

    from nanolab.config.one_shot import ArtifactReference
    from nanolab.plans import one_shot_common as common
    from nanolab.tasks.resource_tasks import acquire_now

    path, components, _, _ = archive(tmp_path)
    path = path.rename(tmp_path / "images-arm64.tar")
    report = tmp_path / "distribution.json"
    report.write_text('{"tag":"built-once"}')
    reference = ArtifactReference(
        path=report, sha256=hashlib.sha256(report.read_bytes()).hexdigest()
    )
    checked = SimpleNamespace(
        report=report,
        components=[
            SimpleNamespace(image=SimpleNamespace(reference=key, id=value))
            for key, value in components.items()
        ],
    )
    monkeypatch.setattr(common, "read_distribution", lambda *a, **kw: checked)
    monkeypatch.setattr(
        common, "require_validation_distribution", lambda *a, **kw: None
    )

    def forbidden(*args, **kwargs):
        pytest.fail("prebuilt distribution must not invoke Docker or compile")

    monkeypatch.setattr(common.subprocess, "run", forbidden)
    monkeypatch.setattr(common, "recipe_run_resource", forbidden)
    if fault == "digest":
        checked.components[0].image.id = "sha256:" + "0" * 64
    if fault == "missing":
        path.unlink()
    architecture = "amd64" if fault == "architecture" else "arm64"
    if fault == "architecture":
        path.rename(tmp_path / "images-amd64.tar")
    distributions, archives = common._architecture_images(
        SimpleNamespace(runtime_distributions={architecture: reference}),
        recipe=tmp_path / "recipe.yaml",
        selected=(),
        run_dir=tmp_path,
        architectures={architecture},
    )
    resource = distributions[architecture]
    built = acquire_now(resource, TaskInputs._for_resources({}, set()))
    inputs = TaskInputs._for_resources({resource: built}, {resource})
    if fault:
        with pytest.raises((ValueError, FileNotFoundError)):
            acquire_now(archives[architecture], inputs)
    else:
        assert acquire_now(archives[architecture], inputs) == path
        assert acquire_now(archives[architecture], inputs) == path


@pytest.mark.parametrize("identity", ["config", "manifest", "index"])
@pytest.mark.parametrize(
    "fault", [None, "config", "manifest", "architecture", "platforms"]
)
def test_containerd_index_binds_runtime_config_and_architecture(
    tmp_path, identity, fault
):
    path, _, config, manifest = archive(tmp_path, corrupt=fault == "manifest")
    with tarfile.open(path) as source:
        entries = {}
        for member in source:
            content = source.extractfile(member)
            assert content is not None
            entries[member.name] = content.read()
    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": manifest,
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
                {
                    "digest": "sha256:" + "f" * 64,
                    "annotations": {
                        "vnd.docker.reference.type": "attestation-manifest"
                    },
                },
            ],
        }
    ).encode()
    if fault == "platforms":
        multiple = json.loads(index)
        multiple["manifests"].append(multiple["manifests"][0])
        index = json.dumps(multiple).encode()
    index_id = "sha256:" + hashlib.sha256(index).hexdigest()
    entries["blobs/sha256/" + index_id[7:]] = index
    entries["index.json"] = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": index_id,
                    "annotations": {"io.containerd.image.name": "fn:tag"},
                }
            ],
        }
    ).encode()
    if fault == "config":
        entries["blobs/sha256/" + config[7:]] = b'{"architecture":"amd64"}'
    with tarfile.open(path, "w") as output:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            output.addfile(info, io.BytesIO(content))
    expected = {
        "fn:tag": {"config": config, "manifest": manifest, "index": index_id}[identity]
    }
    if fault:
        with pytest.raises(
            ValueError,
            match={"architecture": "architecture", "platforms": "platform"}.get(
                fault, "digest"
            ),
        ):
            archive_image_ids(
                path,
                expected,
                architecture="amd64" if fault == "architecture" else "arm64",
            )
    else:
        assert archive_image_ids(path, expected, architecture="arm64") == {
            "fn:tag": {config, manifest, index_id}
        }
