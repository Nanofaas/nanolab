"""Synthetic validation of owned, pre-existing tmpfs Docker volumes."""

from dataclasses import replace

import pytest

from nanolab.tasks.soak.diagnostic_helper import (
    DockerHelperSpec,
    helper_create_argv,
    validate_target_tmp_volume,
)
from nanolab.tasks.soak.models import Target


def fixture(tmp_path):
    spec = DockerHelperSpec(
        target=Target(
            "jvm", "a" * 64, 1234, "started", "target@sha256:" + "b" * 64, "jvm"
        ),
        helper_image="helper@sha256:" + "c" * 64,
        owner_label="nanolab.run",
        owner_value="owned-run",
        uid=65532,
        gid=65532,
        output_root=tmp_path,
        quota_bytes=16777216,
        helper_memory_bytes=134217728,
        allow_target_stop_on_cancel=True,
        target_tmp_volume="owned-tmp",
    )
    volume = {
        "Name": "owned-tmp",
        "Driver": "local",
        "Scope": "local",
        "CreatedAt": "2026-09-13T15:00:00Z",
        "Mountpoint": "/docker/volumes/owned-tmp/_data",
        "Labels": {"nanolab.run": "owned-run", "nanolab.diagnostic.tmp": "true"},
        "Options": {
            "type": "tmpfs",
            "device": "tmpfs",
            "o": "size=33554432,uid=65532,gid=65532,mode=1777,noexec,nosuid,nodev",
        },
    }
    target = {
        "Mounts": [
            {
                "Type": "volume",
                "Name": "owned-tmp",
                "Driver": "local",
                "Destination": "/tmp",
                "Source": volume["Mountpoint"],
                "RW": True,
            }
        ]
    }
    return spec, volume, target


def test_existing_owned_tmpfs_is_mounted_by_name_without_proc_bind(tmp_path):
    spec, volume, target = fixture(tmp_path)
    assert validate_target_tmp_volume(spec, volume, target) == 33554432
    argv = helper_create_argv(spec, "helper")
    assert "type=volume,source=owned-tmp,target=/tmp,volume-nocopy" in argv
    assert not any("source=/proc/" in arg for arg in argv)
    assert "volume" not in argv  # No volume create operation.


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("Name", "unowned"),
        ("Driver", "nfs"),
        ("Scope", "global"),
        ("Labels", {}),
        ("CreatedAt", ""),
        ("Options", {"type": "none", "device": "/host/path", "o": "bind"}),
    ],
)
def test_volume_identity_driver_and_ownership_are_required(tmp_path, key, value):
    spec, volume, target = fixture(tmp_path)
    volume[key] = value
    with pytest.raises(ValueError, match=r"existing exact owned local tmpfs"):
        validate_target_tmp_volume(spec, volume, target)


@pytest.mark.parametrize(
    "options",
    [
        "uid=65532,gid=65532,mode=1777,noexec,nosuid,nodev",
        "size=0,uid=65532,gid=65532,mode=1777,noexec,nosuid,nodev",
        "size=999999999999999999999,uid=65532,gid=65532,mode=1777,noexec,nosuid,nodev",
        "size=50%,uid=65532,gid=65532,mode=1777,noexec,nosuid,nodev",
        "size=33554432,size=4096,uid=65532,gid=65532,mode=1777,noexec,nosuid,nodev",
        "size=33554432,uid=0,gid=65532,mode=1777,noexec,nosuid,nodev",
        "size=33554432,uid=65532,gid=65532,mode=1777,nosuid,nodev",
        "size=33554432,uid=65532,gid=65532,mode=1777,noexec,nosuid,nodev,bind",
    ],
)
def test_tmpfs_options_are_explicit_finite_and_restrictive(tmp_path, options):
    spec, volume, target = fixture(tmp_path)
    volume["Options"]["o"] = options
    with pytest.raises(
        ValueError,
        match=(
            r"duplicate tmpfs mount|explicit finite restrictive|target tmpfs capacity"
        ),
    ):
        validate_target_tmp_volume(spec, volume, target)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("Type", "bind"),
        ("Name", "other-volume"),
        ("Driver", "other"),
        ("RW", False),
        ("Source", "/host/tmp"),
        ("Destination", "/elsewhere"),
    ],
)
def test_target_actual_mount_must_be_same_writable_volume(tmp_path, key, value):
    spec, volume, target = fixture(tmp_path)
    target["Mounts"][0][key] = value
    with pytest.raises(ValueError, match="actual"):
        validate_target_tmp_volume(spec, volume, target)


def test_diagnostic_volume_required_but_memory_only_needs_no_volume(tmp_path):
    spec, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match=r"diagnostics require an explicit owned"):
        replace(spec, target_tmp_volume=None)
    with pytest.raises(ValueError, match=r"diagnostics require an explicit owned"):
        replace(spec, target_tmp_volume="/host/tmp")
    memory = replace(
        spec,
        memory_only=True,
        target_tmp_volume=None,
        allow_target_stop_on_cancel=False,
    )
    assert "--mount" not in helper_create_argv(memory, "memory")


def test_jvm_shared_tmp_must_fit_output_reservation_before_helper_creation(tmp_path):
    import json

    from nanolab.tasks.soak import diagnostic_helper as helper

    spec, volume, target = fixture(tmp_path)

    class Commands:
        def run(self, args):
            assert args == ("volume", "inspect", "owned-tmp")
            return json.dumps([volume])

    with pytest.raises(ValueError, match=r"shared JVM tmpfs quota exceeds the"):
        helper._inspect_target_tmp_volume(Commands(), spec, target)
