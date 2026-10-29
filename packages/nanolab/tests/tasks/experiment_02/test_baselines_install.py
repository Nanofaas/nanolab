"""Baselines are installed once per identity, verified, and never silently stale."""

import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml

from nanolab.tasks.experiment_02 import baselines
from nanolab.tasks.experiment_02.baselines import (
    FNSERVER,
    baseline_identity,
    ensure_baselines,
    install_baselines,
)
from nanolab.tasks.experiment_02.workload import ARTIFACTS, ensure_workloads
from nanolab.workspace.paths import bundled_assets_root

from .fakes import RecordingProvider, Result, vm

SOURCE = PurePosixPath("/home/azureuser/e02/workloads/qr-code")
ARTIFACT_SHA = {key: f"{index}" * 64 for index, key in enumerate(ARTIFACTS)}
TREE = "9" * 40


def _scripts(provider: RecordingProvider) -> str:
    return "\n".join(
        argv[-1] for argv in provider.commands() if argv[:2] == ("sudo", "sh")
    )


def _installer() -> RecordingProvider:
    def handler(argv, _env):
        if argv[:2] == ("sudo", "touch"):
            provider.files[argv[2]] = b""

    provider = RecordingProvider(handler=handler)
    return provider


def test_install_verifies_spin_checksum_and_image_digests():
    provider = _installer()
    install_baselines(
        vm(provider),
        ("spin", "funless", "fn"),
        source=SOURCE,
        artifacts=ARTIFACT_SHA,
        workload_tree=TREE,
    )
    scripts = _scripts(provider)
    assert "checksums-v4.2.5.txt" in scripts and "sha256sum -c" in scripts
    assert "funlessdev/core@sha256:c75155af" in scripts
    assert FNSERVER in scripts
    assert "tinyfaas" not in scripts  # only the selected platforms


def test_install_is_idempotent_per_identity():
    provider = _installer()
    first = install_baselines(
        vm(provider), ("fn",), source=SOURCE, artifacts=ARTIFACT_SHA, workload_tree=TREE
    )
    count = len(provider.commands())
    again = install_baselines(
        vm(provider), ("fn",), source=SOURCE, artifacts=ARTIFACT_SHA, workload_tree=TREE
    )
    assert again["fn"]["root"] == first["fn"]["root"]
    assert all(argv[:2] != ("sudo", "sh") for argv in provider.commands()[count:])
    # Other wrappers are another installation, beside the first, never over it.
    changed = install_baselines(
        vm(provider),
        ("fn",),
        source=SOURCE,
        artifacts={**ARTIFACT_SHA, "qr-fn": "f" * 64},
        workload_tree=TREE,
    )
    assert changed["fn"]["root"] != first["fn"]["root"]
    assert changed["fn"]["image"] != first["fn"]["image"]
    assert str(first["fn"]["image"]).endswith(str(first["fn"]["identity"]))


def test_install_records_what_it_observed():
    def handler(argv, _env):
        if argv[:2] == ("sh", "-c") and "RepoDigests" in argv[-1]:
            return Result(0, "fnproject/fnserver@sha256:16dedb58\n")
        if argv[:2] == ("sudo", "touch"):
            provider.files[argv[2]] = b""
        return None

    provider = RecordingProvider(handler=handler)
    installed = install_baselines(
        vm(provider), ("fn",), source=SOURCE, artifacts=ARTIFACT_SHA, workload_tree=TREE
    )
    assert "fnserver@sha256:16dedb58" in str(installed["fn"]["observed"])


def test_identity_covers_the_install_script_and_the_wrappers(tmp_path, monkeypatch):
    def identity(key: str = "funless", tree: str = TREE) -> str:
        return baseline_identity(key, artifacts=ARTIFACT_SHA, workload_tree=tree)

    first = identity()
    assert identity() == first
    # spin.toml, fn.sh and the Dockerfiles are in the workload tree.
    assert identity(tree="8" * 40) != first
    # The FunLess compose file is part of the install script.
    assets = tmp_path / "assets"
    (assets / "experiment02").mkdir(parents=True)
    (assets / "experiment02" / "funless-compose.yaml").write_text("services: {}\n")
    monkeypatch.setattr(baselines, "bundled_assets_root", lambda: assets)
    assert identity() != first
    # So is every command, here the Go toolchain Serverledge builds with.
    before = identity("serverledge")
    monkeypatch.setattr(baselines, "GOLANG", "golang:1.25")
    assert identity("serverledge") != before


def _ensure(provider: RecordingProvider, record: Path, **changes):
    arguments = {"artifacts": ARTIFACT_SHA, "workload_tree": TREE, **changes}
    return ensure_baselines(
        vm(provider), ("fn",), source=SOURCE, record=record, **arguments
    )


def test_funless_runs_only_the_images_it_pulls_and_records_them_by_digest():
    compose = yaml.safe_load(
        (bundled_assets_root() / "experiment02" / "funless-compose.yaml").read_text()
    )
    images = {service["image"] for service in compose["services"].values()}

    assert images == set(baselines._PULLED["funless"])
    assert all("@sha256:" in image for image in images)


def test_a_resume_reuses_the_recorded_installation(tmp_path):
    provider = _installer()
    record = tmp_path / "prepare" / "baselines.json"
    first = _ensure(provider, record)
    assert json.loads(record.read_text()) == first
    count = len(provider.commands())
    assert _ensure(provider, record) == first
    assert all(argv[:2] != ("sudo", "sh") for argv in provider.commands()[count:])


def test_a_resume_with_other_inputs_is_refused_before_installing(tmp_path):
    provider = _installer()
    record = tmp_path / "baselines.json"
    _ = _ensure(provider, record)
    count = len(provider.commands())
    with pytest.raises(RuntimeError, match="installation changed"):
        _ensure(provider, record, workload_tree="8" * 40)
    assert provider.commands()[count:] == []


def test_a_resume_whose_installation_drifted_is_refused(tmp_path):
    digest = ["fnproject/fnserver@sha256:16dedb58\n"]

    def handler(argv, _env):
        if argv[:2] == ("sh", "-c") and "RepoDigests" in argv[-1]:
            return Result(0, digest[0])
        if argv[:2] == ("sudo", "touch"):
            provider.files[argv[2]] = b""
        return None

    provider = RecordingProvider(handler=handler)
    record = tmp_path / "baselines.json"
    _ = _ensure(provider, record)
    digest[0] = "fnproject/fnserver@sha256:0000\n"
    with pytest.raises(RuntimeError, match="installation changed"):
        _ensure(provider, record)


def _run_script(tmp_path: Path, monkeypatch, key: str, fail: str) -> tuple[int, str]:
    """Run a generated install script for real, with fake tools; `fail` exits 1."""
    calls = tmp_path / "calls"
    tools = tmp_path / "bin"
    tools.mkdir()
    for tool in ("curl", "grep", "sha256sum", "tar", "cp", "docker", "apt-get"):
        code = 1 if tool == fail else 0
        script = f'#!/bin/sh\necho {tool} "$@" >> {calls}\n'
        if tool == "apt-get" and code == 0:
            script += (
                f'case "$*" in *docker-compose-v2*) touch "{tmp_path}/compose";; esac\n'
            )
        if tool == "docker":
            script += (
                f'if [ "$1" = compose ] && [ ! -f "{tmp_path}/compose" ]; '
                "then exit 125; fi\n"
            )
        (tools / tool).write_text(script + f"exit {code}\n")
        (tools / tool).chmod(0o755)
    root = tmp_path / "opt" / key / "x"
    root.mkdir(parents=True)
    script = baselines._install_script(key, SOURCE, PurePosixPath(root), "x")
    done = subprocess.run(
        ("sh", "-c", f"set -e\n{script}"),
        env={**os.environ, "PATH": f"{tools}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, calls.read_text() if calls.exists() else ""


@pytest.mark.parametrize(
    ("key", "fail", "never"),
    [
        ("spin", "sha256sum", "tar "),  # a checksum mismatch never extracts
        ("spin", "curl", "tar "),
        ("fn", "docker", "docker push"),  # a failed pull or build never pushes
        ("funless", "apt-get", "docker pull"),
    ],
)
def test_a_failed_step_stops_the_install(tmp_path, monkeypatch, key, fail, never):
    code, calls = _run_script(tmp_path, monkeypatch, key, fail)
    assert code != 0
    assert never not in calls


def test_a_failed_install_writes_no_marker():
    def handler(argv, _env):
        if argv[:2] == ("sudo", "sh"):
            return Result(1, "", "checksum mismatch")
        return None

    provider = RecordingProvider(handler=handler)
    with pytest.raises(RuntimeError):
        install_baselines(
            vm(provider),
            ("spin",),
            source=SOURCE,
            artifacts=ARTIFACT_SHA,
            workload_tree=TREE,
        )
    assert not any(argv[:2] == ("sudo", "touch") for argv in provider.commands())


def test_resume_refuses_changed_artifacts(tmp_path: Path):
    record = tmp_path / "workloads.json"
    record.write_text(json.dumps(ARTIFACT_SHA))
    provider = RecordingProvider()
    for key, path in ARTIFACTS.items():
        provider.files[str(SOURCE / path)] = key.encode()  # hashes differ
    with pytest.raises(ValueError, match="changed"):
        ensure_workloads(
            vm(provider),
            workloads=tmp_path,
            remote_root=SOURCE.parent,
            payload=b"{}",
            png_sha256=None,
            record=record,
        )


@pytest.mark.parametrize("key", ["fn", "serverledge"])
def test_cached_install_republishes_its_image_into_a_new_registry(key):
    published = set()

    def handler(argv, _env):
        if argv[:2] == ("sudo", "touch"):
            provider.files[argv[2]] = b""
        if argv[:2] == ("docker", "push"):
            published.add(argv[2])

    provider = RecordingProvider(handler=handler)
    arguments = {"source": SOURCE, "artifacts": ARTIFACT_SHA, "workload_tree": TREE}
    first = install_baselines(vm(provider), (key,), **arguments)
    # A new run recreates the registry but preserves the installation marker.
    published.clear()
    again = install_baselines(vm(provider), (key,), **arguments)
    assert again == first
    assert published == {first[key]["image"]}


@pytest.mark.parametrize("key", ["fn", "serverledge"])
def test_cached_install_refuses_when_its_image_cannot_be_republished(key):
    provider = _installer()
    arguments = {"source": SOURCE, "artifacts": ARTIFACT_SHA, "workload_tree": TREE}
    install_baselines(vm(provider), (key,), **arguments)

    def missing_image(argv, _env):
        if argv[:2] == ("docker", "push"):
            return Result(1, "", "image does not exist locally")
        return None

    provider.handler = missing_image
    with pytest.raises(RuntimeError, match="image does not exist locally"):
        install_baselines(vm(provider), (key,), **arguments)


def test_tinyfaas_refreshes_packages_before_installing_toolchain():
    provider = _installer()
    install_baselines(
        vm(provider),
        ("tinyfaas",),
        source=SOURCE,
        artifacts=ARTIFACT_SHA,
        workload_tree=TREE,
    )
    script = _scripts(provider)
    assert script.index("apt-get update") < script.index("apt-get install")


def test_fresh_funless_preparation_makes_compose_available(tmp_path, monkeypatch):
    code, _calls = _run_script(tmp_path, monkeypatch, "funless", "none")
    assert code == 0
    probe = subprocess.run(
        (str(tmp_path / "bin/docker"), "compose", "version"), check=False
    )
    assert probe.returncode == 0


def test_version_probe_failure_cannot_be_recorded_as_a_valid_installation():
    def handler(argv, _env):
        if argv[:2] == ("sudo", "touch"):
            provider.files[argv[2]] = b""
        if argv[:2] == ("sh", "-c") and "docker compose version" in argv[-1]:
            return Result(125, "", "docker compose unavailable")
        return None

    provider = RecordingProvider(handler=handler)
    with pytest.raises(RuntimeError, match="compose unavailable"):
        install_baselines(
            vm(provider),
            ("funless",),
            source=SOURCE,
            artifacts=ARTIFACT_SHA,
            workload_tree=TREE,
        )


@pytest.mark.parametrize("key", ["tinyfaas", "serverledge"])
def test_git_revision_probe_reads_the_root_owned_installation(key):
    revision = baselines._GIT[key]

    def handler(argv, _env):
        if argv[:2] == ("sudo", "touch"):
            provider.files[argv[2]] = b""
        if argv[:2] == ("sh", "-c") and "rev-parse HEAD" in argv[-1]:
            if "sudo git -C " not in argv[-1]:
                return Result(
                    128, "", "fatal: detected dubious ownership in repository"
                )
            return Result(0, revision + "\n")
        return None

    provider = RecordingProvider(handler=handler)
    installed = install_baselines(
        vm(provider), (key,), source=SOURCE, artifacts=ARTIFACT_SHA, workload_tree=TREE
    )
    assert installed[key]["observed"].strip() == revision
