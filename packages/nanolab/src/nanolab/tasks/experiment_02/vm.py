"""One VM of experiment 02, reached through the environment's command provider.

The transport is the comparison's own. A command that must succeed goes through
`comparison_remote_command`, a file through the verified
`comparison_transfer_to` or `comparison_transfer_from`. A command whose exit
status is the answer goes through the provider's `exec_argv`, and its Sonata
result is returned unchanged. A `Vm` only names the machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sonata_tasks.transfer import RemoteOperationResult
from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.comparison.prepare import comparison_transfer_from
from nanolab.tasks.vm.models import VmRequest


@dataclass(frozen=True)
class Vm:
    """The provider that reaches a machine, and the request that names it."""

    provider: VmCommandProvider
    request: VmRequest


def exec_status(vm: Vm, argv: tuple[str, ...]) -> RemoteOperationResult:
    """Run `argv` and return the provider's result, whatever its exit status."""
    return vm.provider.exec_argv(
        vm.request, argv, env=None, remote_dir=None, dry_run=False
    )


def fetch_if_present(vm: Vm, remote: PurePosixPath, local: Path) -> bool:
    """Download `remote` when it exists; a missing file is an answer, not an error."""
    if exec_status(vm, ("test", "-f", str(remote))).return_code:
        return False
    comparison_transfer_from(vm.provider, vm.request, remote, local)
    return True
