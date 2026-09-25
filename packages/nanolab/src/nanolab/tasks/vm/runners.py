"""Remote-fetch adapter bridging a VM command provider and the load-test port."""

from __future__ import annotations

from pathlib import Path

from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.tasks.vm.models import VmRequest


class VmFileFetcher:
    """Implements RemoteFileFetcher using any provider's transfer_from()."""

    def __init__(self, vm: VmCommandProvider, request: VmRequest) -> None:
        """Store the provider that performs transfers and the target request."""
        self._vm = vm
        self._request = request

    def fetch_from(self, remote: str, local: Path) -> None:
        """Copy the VM's `remote` path to local `local`, raising on failure."""
        result = self._vm.transfer_from(self._request, source=remote, destination=local)
        return_code = getattr(result, "return_code", 0)
        if return_code != 0:
            stderr = getattr(result, "stderr", "") or ""
            stdout = getattr(result, "stdout", "") or ""
            raise RuntimeError(
                stderr or stdout or f"transfer failed (exit {return_code})"
            )
