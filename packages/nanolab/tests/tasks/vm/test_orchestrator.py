from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from multipass_vm_sdk import VmNotFoundError
from multipass_vm_sdk.models import VmState
from sonata_tasks.shell import RecordingShell, ShellBackend
from sonata_tasks.vm.models import VmRequest

from nanolab.tasks.vm.orchestrator import VmOrchestrator


def _make_orch(
    repo_root: Path = Path("/repo"),
    shell: ShellBackend | None = None,
    multipass_client: object | None = None,
) -> VmOrchestrator:
    """Build a VmOrchestrator with controllable dependencies."""
    if shell is None:
        shell = RecordingShell()
    mock_client = multipass_client if multipass_client is not None else MagicMock()
    return VmOrchestrator(
        repo_root=repo_root,
        shell=shell,
        multipass_client=mock_client,
    )


def test_vm_exists_only_treats_not_found_as_absent() -> None:
    client = MagicMock()
    client.get_vm.return_value.info.return_value.state.value = "running"
    provider = _make_orch(multipass_client=client)
    request = VmRequest(lifecycle="multipass", name="stack")
    assert provider.vm_exists(request) is True
    client.get_vm.side_effect = VmNotFoundError("stack")
    assert provider.vm_exists(request) is False


def test_vm_exists_refuses_deleted_instance_global_purge() -> None:
    client = MagicMock()
    client.get_vm.return_value.info.return_value.state = VmState.DELETED
    provider = _make_orch(multipass_client=client)
    with pytest.raises(RuntimeError, match="refusing global purge"):
        provider.vm_exists(VmRequest(lifecycle="multipass", name="stack"))


def test_remote_project_dir_uses_nanofaas_suffix() -> None:
    orch = VmOrchestrator(repo_root=Path("/repo"), shell=RecordingShell())
    request = VmRequest(lifecycle="external", host="vm.example.test", user="dev")
    assert orch.remote_project_dir(request).endswith("/nanofaas")
