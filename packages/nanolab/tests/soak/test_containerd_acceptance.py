"""Offline provenance must bind the live process artifact and OCI functions."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.soak.acceptance import verify_containerd_builds
from nanolab.tasks.soak.artifacts import describe_artifact
from nanolab.tasks.soak.sources import SourceEntry


def _case(tmp_path):
    scenario = (
        Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    )
    config = ScenarioConfig.model_validate(yaml.safe_load(scenario.read_text())).soak
    assert config is not None
    entry = SourceEntry("x", "file", 0o644, 1, "a" * 64)
    snapshot = SimpleNamespace(
        fingerprint="source-hash",
        revision="commit123",
        dirty=False,
        entries=(entry,),
    )
    batch_hash = hashlib.sha256(
        json.dumps([asdict(entry)], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    remote = {
        "schema": "nanolab-containerd-source-v1",
        "revision": "commit123",
        "clean": True,
        "source_fingerprint": "source-hash",
        "entry_count": 1,
        "batch_size": 50,
        "batches": [batch_hash],
        "verification": "remote-content-after-build; rsync excludes .git",
    }
    remote_path = tmp_path / "remote-source.json"
    remote_path.write_text(json.dumps(remote))
    manifest = {
        "remote_source": {
            "path": remote_path.name,
            "sha256": describe_artifact(remote_path)["sha256"],
        },
        "builds": {},
        "recipes": {},
    }
    observed = {"remote_source": remote, "build_receipts": {}, "roles": {}}
    targets = {}
    for role, spec in config.images.items():
        process = role == "control-plane"
        digest = (
            "sha256:" + "a" * 64 if process else "127.0.0.1:5000/fn@sha256:" + "b" * 64
        )
        path = (
            "/home/ubuntu/nanofaas/platform/control-plane/build/libs/app.jar"
            if process
            else None
        )
        command = (
            [
                "./gradlew",
                ":control-plane:bootJar",
                "-PcontrolPlaneModules=" + ",".join(spec.modules),
            ]
            if process
            else ["docker", "build", "-t", "127.0.0.1:5000/fn:soak-run123", "."]
        )
        receipt = {
            "schema": "nanolab-containerd-build-v1",
            "role": role,
            "image_digest": digest,
            "source_fingerprint": "source-hash",
            "source_revision": "commit123",
            "platform": spec.platform,
            "artifact_kind": spec.artifact_kind,
            "artifact_path": path,
            "build_argv": command,
        }
        recipe = {
            "role": role,
            "artifact_kind": spec.artifact_kind,
            "platform": spec.platform,
            "build_argv": command,
        }
        for key, value in (("builds", receipt), ("recipes", recipe)):
            file = tmp_path / f"{key}-{role}.json"
            file.write_text(json.dumps(value))
            manifest[key][role] = {
                "path": file.name,
                "sha256": describe_artifact(file)["sha256"],
            }
        observed["build_receipts"][role] = {
            k: v for k, v in receipt.items() if k != "schema"
        }
        observed["roles"][role] = {"artifact_path": path, "platform": spec.platform}
        targets[role] = {"image_digest": digest}
    return config, snapshot, manifest, observed, targets


def test_containerd_build_receipts_bind_real_process_and_images(tmp_path):
    config, source, manifest, observed, targets = _case(tmp_path)
    assert "systemd process" in verify_containerd_builds(
        tmp_path, manifest, config, targets, source, observed
    )


def test_containerd_build_receipts_reject_unused_cp_image_claim(tmp_path):
    config, source, manifest, observed, targets = _case(tmp_path)
    targets["control-plane"]["image_digest"] = "127.0.0.1:5000/cp@sha256:" + "a" * 64
    with pytest.raises(ValueError, match="process artifact digest"):
        verify_containerd_builds(tmp_path, manifest, config, targets, source, observed)


def test_containerd_build_receipts_reject_different_repository_on_same_registry(
    tmp_path,
):
    config, source, manifest, observed, targets = _case(tmp_path)
    receipt_ref = manifest["builds"]["word-stats-java"]
    recipe_ref = manifest["recipes"]["word-stats-java"]
    for ref in (receipt_ref, recipe_ref):
        path = tmp_path / ref["path"]
        value = json.loads(path.read_text())
        value["build_argv"] = [
            "docker",
            "build",
            "-t",
            "127.0.0.1:5000/foreign:soak-run123",
            ".",
        ]
        path.write_text(json.dumps(value))
        ref["sha256"] = describe_artifact(path)["sha256"]
    observed["build_receipts"]["word-stats-java"]["build_argv"] = [
        "docker",
        "build",
        "-t",
        "127.0.0.1:5000/foreign:soak-run123",
        ".",
    ]
    with pytest.raises(ValueError, match="repository"):
        verify_containerd_builds(tmp_path, manifest, config, targets, source, observed)


def test_containerd_build_receipts_reject_unverified_staged_source(tmp_path):
    config, source, manifest, observed, targets = _case(tmp_path)
    path = tmp_path / manifest["remote_source"]["path"]
    remote = json.loads(path.read_text())
    remote["batches"] = ["0" * 64]
    path.write_text(json.dumps(remote))
    manifest["remote_source"]["sha256"] = describe_artifact(path)["sha256"]
    observed["remote_source"] = remote
    with pytest.raises(ValueError, match="staged source"):
        verify_containerd_builds(tmp_path, manifest, config, targets, source, observed)
