"""One VM of experiment 02, reached through the environment's command provider."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.tasks.vm.models import VmRequest


@dataclass(frozen=True)
class Vm:
    """Run commands on, and move files to and from, one machine."""

    provider: VmCommandProvider
    request: VmRequest

    def probe(
        self, argv: tuple[str, ...], *, env: Mapping[str, str] | None = None
    ) -> tuple[int, str, str]:
        """Run `argv` and return its exit status and output, whatever they are."""
        result = self.provider.exec_argv(
            self.request,
            argv,
            env=dict(env) if env is not None else None,
            remote_dir=None,
            dry_run=False,
        )
        return result.return_code, result.stdout, result.stderr

    def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str] | None = None
    ) -> str:
        """Run `argv` and return its output, failing closed on a nonzero status."""
        code, stdout, stderr = self.probe(argv, env=env)
        if code:
            raise RuntimeError(
                f"experiment-02 command failed on {self.request.name}: "
                f"{argv[0]}: {stderr.strip()}"
            )
        return stdout

    def put(self, local: Path, remote: PurePosixPath) -> None:
        """Copy `local` to `remote` and require the copy to hash the same."""
        result = self.provider.transfer_to(
            self.request, source=local, destination=str(remote)
        )
        if result.return_code:
            raise RuntimeError(f"transfer to {self.request.name} failed: {remote}")
        expected = hashlib.sha256(local.read_bytes()).hexdigest()
        actual = self.run(("sha256sum", str(remote))).split()
        if not actual or actual[0] != expected:
            raise ValueError(f"transferred file differs from its source: {remote}")

    def get(self, remote: PurePosixPath, local: Path) -> bool:
        """Copy `remote` to `local` if it exists; say whether it did."""
        if self.probe(("test", "-f", str(remote)))[0]:
            return False
        local.parent.mkdir(parents=True, exist_ok=True)
        result = self.provider.transfer_from(
            self.request, source=str(remote), destination=local
        )
        if result.return_code:
            raise RuntimeError(f"transfer from {self.request.name} failed: {remote}")
        return True
