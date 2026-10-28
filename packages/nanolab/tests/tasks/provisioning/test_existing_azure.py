"""Existing Azure targets are verified without acquiring OpenTofu ownership."""

import json
import shlex
import subprocess
import sys
import time

import pytest

from nanolab.tasks.provisioning.providers import provider_for
from nanolab.tasks.vm.azure_existing import ExistingAzureVmProvider
from nanolab.tasks.vm.models import VmRequest


def request():
    return VmRequest(
        lifecycle="azure",
        name="existing",
        host="203.0.113.8",
        user="azureuser",
        azure_resource_group="rg",
        azure_location="westeurope",
        azure_vm_size="Standard_D8s_v5",
        azure_image_urn="Canonical:ubuntu:server:fixed",
    )


def metadata():
    return {
        "powerState": "VM running",
        "publicIps": "203.0.113.8",
        "location": "westeurope",
        "vmSize": "Standard_D8s_v5",
        "publisher": "Canonical",
        "offer": "ubuntu",
        "sku": "server",
        "version": "fixed",
    }


def test_explicit_azure_host_uses_read_only_existing_target(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(metadata()), "")

    monkeypatch.setattr(subprocess, "run", run)
    provider = provider_for(request(), tmp_path)
    assert isinstance(provider, ExistingAzureVmProvider)
    assert provider.ensure_running(request()).return_code == 0
    assert provider.connection_host(request()) == "203.0.113.8"
    assert all(argv[:3] == ["az", "vm", "show"] for argv in calls)
    assert provider.teardown(request()).return_code != 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("powerState", "VM deallocated"),
        ("publicIps", "203.0.113.9"),
        ("vmSize", "Standard_B1s"),
        ("version", "other"),
    ],
)
def test_existing_target_refuses_mismatched_live_facts(
    tmp_path, monkeypatch, field, value
):
    observed = metadata()
    observed[field] = value
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(observed),
            "",
        ),
    )
    with pytest.raises(ValueError, match="existing Azure"):
        ExistingAzureVmProvider(tmp_path).ensure_running(request())


def test_ssh_preserves_argument_environment_and_directory_without_reporting_secrets(
    tmp_path, monkeypatch
):
    real_run = subprocess.run
    remote = tmp_path / "remote directory"
    remote.mkdir()
    marker = tmp_path / "injected"
    token = f"opaque ' \" $(touch {marker})"

    def run(argv, **kwargs):
        assert argv[0] == "ssh"
        command = shlex.split(argv[-1])
        return real_run(command, capture_output=True, text=True, timeout=5)

    monkeypatch.setattr(subprocess, "run", run)
    argv = (
        sys.executable,
        "-c",
        "import os; print(os.environ['TOKEN']); print(os.getcwd())",
    )
    result = ExistingAzureVmProvider(tmp_path).exec_argv(
        request(),
        argv,
        env={"TOKEN": token},
        remote_dir=str(remote),
    )
    assert result.return_code == 0
    assert result.stdout.splitlines() == [token, str(remote)]
    assert not marker.exists()
    assert token not in repr(result.command)


def test_timeout_does_not_retain_transport_secrets(tmp_path, monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(TimeoutError) as error:
        ExistingAzureVmProvider(tmp_path).exec_argv(
            request(),
            ("true",),
            env={"TOKEN": "opaque-test-secret"},
        )
    assert error.value.__context__ is None


def test_transfers_use_the_configured_key_and_never_a_workspace(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    target = request().model_copy(
        update={"azure_ssh_key_path": str(tmp_path / "operator.pub")}
    )
    provider = ExistingAzureVmProvider(tmp_path)
    assert (
        provider.transfer_to(
            target, source=tmp_path / "file", destination="/tmp/file"
        ).return_code
        == 0
    )
    assert (
        provider.transfer_from(
            target, source="/tmp/file", destination=tmp_path / "copy"
        ).return_code
        == 0
    )
    assert all(
        argv[0] == "scp" and str(tmp_path / "operator") in argv for argv in calls
    )


@pytest.mark.parametrize("transport", ["ssh", "scp"])
def test_retry_only_pre_authentication_connection_resets(monkeypatch, transport):
    calls = []
    delays = []
    monkeypatch.setattr(time, "sleep", delays.append)

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                argv,
                255,
                "",
                "kex_exchange_identification: read: Connection reset by peer",
            )
        return subprocess.CompletedProcess(argv, 0, "done", "")

    monkeypatch.setattr(subprocess, "run", run)
    result = ExistingAzureVmProvider._execute([transport, "target"])
    assert result.return_code == 0
    assert len(calls) == 2
    assert delays == [5]


@pytest.mark.parametrize(
    ("transport", "stderr", "expected_calls"),
    [
        ("ssh", "kex_exchange_identification: read: Connection reset by peer", 3),
        ("ssh", "Permission denied (publickey)", 1),
        ("ssh", "remote command failed", 1),
        ("az", "kex_exchange_identification: read: Connection reset by peer", 1),
    ],
)
def test_connection_retries_are_bounded_and_exclude_remote_failures(
    monkeypatch, transport, stderr, expected_calls
):
    calls = []
    delays = []
    monkeypatch.setattr(time, "sleep", delays.append)

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 255, "", stderr)

    monkeypatch.setattr(subprocess, "run", run)
    assert ExistingAzureVmProvider._execute([transport, "target"]).return_code == 255
    assert len(calls) == expected_calls
    assert delays == ([5, 10] if expected_calls == 3 else [])
