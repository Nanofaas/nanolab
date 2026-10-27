"""The budget is applied to one cgroup holding the deployment and its engine."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from nanolab.config.experiment_02 import Budget
from nanolab.tasks.experiment_02.budget import (
    SLICE_SETUP_SCRIPT,
    apply_budget_argv,
    parse_budget,
    processes_outside,
    slice_setup_operation,
)


def test_apply_budget_argv():
    assert apply_budget_argv(Budget(cpus=1.5, memoryMiB=1024)) == (
        "sudo",
        "systemctl",
        "set-property",
        "--runtime",
        "nanolab-sut.slice",
        "CPUQuota=150%",
        "MemoryMax=1024M",
        "MemorySwapMax=0",
    )


def test_parse_budget_reads_back_the_cgroup():
    assert parse_budget("150000 100000\n1073741824\n0\n") == Budget(
        cpus=1.5, memoryMiB=1024
    )
    assert parse_budget("200000 100000\n2147483648\n0\n") == Budget(
        cpus=2, memoryMiB=2048
    )


@pytest.mark.parametrize(
    "text",
    ["max 100000\n1073741824\n0\n", "150000 100000\nmax\n0\n"],
)
def test_parse_budget_refuses_an_unbounded_slice(text):
    with pytest.raises(ValueError, match="unbounded"):
        parse_budget(text)


def test_parse_budget_refuses_swap():
    with pytest.raises(ValueError, match="swap"):
        parse_budget("150000 100000\n1073741824\nmax\n")


def _run_setup(tmp_path: Path, daemon_json: str | None = None) -> list[str]:
    """Run the setup script against a fake root, recording every command."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    for command in ("systemctl", "docker", "swapoff"):
        fake = bin_dir / command
        fake.write_text(f'#!/bin/sh\necho "{command} $*" >> "{calls}"\n')
        fake.chmod(0o755)
    if daemon_json is not None:
        (tmp_path / "etc" / "docker").mkdir(parents=True)
        (tmp_path / "etc" / "docker" / "daemon.json").write_text(daemon_json)
    subprocess.run(
        ("sh", "-c", SLICE_SETUP_SCRIPT),
        check=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "NANOLAB_SLICE_ROOT": str(tmp_path),
        },
    )
    return calls.read_text().splitlines()


def test_slice_setup_moves_the_engine_into_the_budget_and_the_registry_out(tmp_path):
    calls = _run_setup(tmp_path)
    units = tmp_path / "etc" / "systemd" / "system"
    for service in ("docker", "containerd"):
        dropin = units / f"{service}.service.d" / "nanolab-slice.conf"
        assert "Slice=nanolab-sut.slice" in dropin.read_text().splitlines()
    assert (units / "nanolab-sut.slice").is_file()
    assert (units / "nanolab-infra.slice").is_file()
    # The agents measure from their own slice, inside infra but apart from the
    # registry.
    assert (units / "nanolab-infra-observers.slice").is_file()
    daemon = json.loads((tmp_path / "etc" / "docker" / "daemon.json").read_text())
    assert daemon["cgroup-parent"] == "nanolab-sut.slice"
    restart = calls.index("systemctl restart containerd docker")
    assert calls.index("systemctl daemon-reload") < restart
    registry = [call for call in calls if call.startswith("docker run")]
    assert len(registry) == 1
    assert "--cgroup-parent=nanolab-infra.slice" in registry[0].split()
    assert calls.index(registry[0]) > restart
    assert "swapoff -a" in calls


def test_slice_setup_keeps_existing_daemon_settings(tmp_path):
    _run_setup(tmp_path, daemon_json='{"log-driver": "local"}\n')
    daemon = json.loads((tmp_path / "etc" / "docker" / "daemon.json").read_text())
    assert daemon == {"cgroup-parent": "nanolab-sut.slice", "log-driver": "local"}


def test_slice_setup_operation_runs_the_script_as_root_on_the_vm():
    operation = slice_setup_operation()
    assert operation.argv == ("sudo", "sh", "-c", SLICE_SETUP_SCRIPT)
    assert operation.execution_target == "vm"


def test_processes_outside_the_slice_are_reported():
    listing = (
        "101 containerd-shim /system.slice/containerd.service\n"
        "102 dockerd /nanolab.slice/nanolab-sut.slice/docker.service\n"
        "103 registry /nanolab.slice/nanolab-infra.slice/docker-ab.scope\n"
        "104 java /nanolab.slice/nanolab-sut.slice/docker-cd.scope\n"
        "105 qr-code /system.slice/docker-ef.scope\n"
        "106 sshd /system.slice/ssh.service\n"
    )
    assert processes_outside(listing) == [
        "101 containerd-shim /system.slice/containerd.service",
        "105 qr-code /system.slice/docker-ef.scope",
    ]
