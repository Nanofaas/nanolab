"""NanoFaaS Multipass paths and guarded VM existence checks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from multipass_vm_sdk import VmNotFoundError
from multipass_vm_sdk.models import VmState
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.providers.multipass import MultipassVmProvider

if TYPE_CHECKING:
    from multipass_vm_sdk import MultipassClient
    from subprocess_toolkit.backend import ShellBackend


__all__ = ["VmOrchestrator"]


class VmOrchestrator(MultipassVmProvider):
    """A Multipass provider with NanoFaaS paths and guarded existence checks."""

    def __init__(
        self,
        repo_root: Path,
        shell: ShellBackend | None = None,
        multipass_client: MultipassClient | None = None,
    ) -> None:
        """Set up the provider with the NanoFaaS checkout as its workspace."""
        self.repo_root = Path(repo_root)
        super().__init__(
            workspace_root=self.repo_root,
            # MultipassVmProvider's ShellBackend argument, not the subprocess
            # shell flag bandit's B604 looks for.
            shell=shell,  # nosec B604
            multipass_client=multipass_client,
        )

    def vm_exists(self, request: VmRequest) -> bool:
        """Check Multipass before ensure; a deleted VM must not trigger purge."""
        if not request.name:
            raise ValueError("managed Multipass VM requires a name")
        try:
            info = self._client.get_vm(request.name).info()
        except VmNotFoundError:
            return False
        if info.state == VmState.DELETED:
            raise RuntimeError(
                f"Multipass VM {request.name} is deleted; refusing global purge"
            )
        return True

    def remote_project_dir(self, request: VmRequest) -> str:
        """Return the in-VM directory the repository is synced into."""
        return f"{self._remote_home(request)}/nanofaas"
