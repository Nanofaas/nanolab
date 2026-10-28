"""Use operator-owned Azure VMs over SSH without creating OpenTofu resources."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import tempfile
import time
from functools import cached_property
from pathlib import Path

from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.providers.azure import AzureVmProvider
from sonata_tasks.vm.results import successful_result
from sonata_tasks.vm.ssh import ssh_command
from subprocess_toolkit.backend import ShellExecutionResult

_QUERY = (
    "{powerState:powerState,publicIps:publicIps,location:location,"
    "vmSize:hardwareProfile.vmSize,publisher:storageProfile.imageReference.publisher,"
    "offer:storageProfile.imageReference.offer,sku:storageProfile.imageReference.sku,"
    "version:storageProfile.imageReference.version}"
)


class ExistingAzureVmProvider(AzureVmProvider):
    """Verify pre-provisioned Azure identities and keep lifecycle ownership external."""

    def connection_host(self, request: VmRequest) -> str:
        """Require an explicit address without consulting a tofu workspace."""
        if not request.host:
            raise ValueError("existing Azure targets require an explicit role host")
        return request.host

    def ensure_running(self, request: VmRequest) -> ShellExecutionResult:
        """Verify the live Azure VM; never create, start or reconfigure it."""
        host = self.connection_host(request)
        if not request.name or not request.azure_resource_group:
            raise ValueError(
                "existing Azure targets require a VM name and resource group"
            )
        result = self._execute(
            [
                "az",
                "vm",
                "show",
                "--show-details",
                "--resource-group",
                request.azure_resource_group,
                "--name",
                request.name,
                "--query",
                _QUERY,
                "--output",
                "json",
            ],
            timeout=60,
        )
        if result.return_code:
            raise RuntimeError(result.stderr or "Unable to verify existing Azure VM")
        actual = json.loads(result.stdout)
        if not isinstance(actual, dict) or actual.get("powerState") != "VM running":
            raise ValueError("existing Azure VM must already be running")
        if host not in str(actual.get("publicIps", "")).split(","):
            raise ValueError("existing Azure role host does not match the live VM")
        if actual.get("vmSize") != request.azure_vm_size:
            raise ValueError("existing Azure VM size does not match the environment")
        if request.azure_location and actual.get("location") != request.azure_location:
            raise ValueError("existing Azure VM region does not match the environment")
        image = ":".join(
            str(actual.get(key)) for key in ("publisher", "offer", "sku", "version")
        )
        if request.azure_image_urn and image != request.azure_image_urn:
            raise ValueError("existing Azure VM image does not match the environment")
        return successful_result(["azure", "existing", request.name])

    def teardown(self, request: VmRequest) -> ShellExecutionResult:
        """Refuse to delete an operator-owned VM through a managed SDK workspace."""
        return ShellExecutionResult(
            command=["azure", "operator-owned", request.name or ""],
            return_code=1,
            stdout="",
            stderr=(
                "Existing Azure targets are operator-owned; "
                "manage their lifecycle explicitly"
            ),
        )

    @staticmethod
    def _execute(
        command: list[str],
        *,
        reported: list[str] | None = None,
        timeout: int = 3600,
    ) -> ShellExecutionResult:
        result = None
        for attempt in range(3):
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                result = None
                break
            # KEX failures precede authentication: the remote command has not run.
            if (
                command[0] not in {"ssh", "scp"}
                or result.returncode != 255
                or "kex_exchange_identification:" not in result.stderr
            ):
                break
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
        if result is None:
            # Raise outside the handler: even __context__ must not retain SSH
            # transport arguments containing the generator credentials.
            raise TimeoutError("Existing Azure command exceeded its timeout")
        return ShellExecutionResult(
            command=command if reported is None else reported,
            return_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    @cached_property
    def _control_directory(self) -> tempfile.TemporaryDirectory[str]:
        # Keep Unix socket paths below OpenSSH's macOS length limit.
        return tempfile.TemporaryDirectory(
            prefix="nanolab-ssh-",
            dir="/tmp",  # nosec B108: TemporaryDirectory securely creates mode 0700.
        )

    def _ssh(self, request: VmRequest) -> list[str]:
        key = str(self.ssh_private_key_path(request))
        identity = hashlib.sha256(key.encode()).hexdigest()[:12]
        socket = f"{self._control_directory.name}/{identity}-%C"
        return [
            *shlex.split(
                ssh_command(private_key_path=self.ssh_private_key_path(request))
            ),
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=60",
            "-o",
            f"ControlPath={socket}",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]

    def exec_argv(
        self,
        request: VmRequest,
        argv: tuple[str, ...] | list[str],
        *,
        env: dict[str, str] | None = None,
        remote_dir: str | None = None,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Run a quoted command using the request's configured SSH identity."""
        if dry_run:
            return successful_result(list(argv))
        script = shlex.join(argv)
        if env:
            script = (
                shlex.join(["env", *(f"{key}={value}" for key, value in env.items())])
                + " "
                + script
            )
        if remote_dir is not None:
            script = f"cd {shlex.quote(remote_dir)} && {script}"
        command = [
            *self._ssh(request),
            "--",
            f"{request.user}@{self.connection_host(request)}",
            shlex.join(("bash", "-lc", script)),
        ]
        return self._execute(command, reported=list(argv))

    def _transfer(
        self,
        request: VmRequest,
        source: str,
        destination: str,
    ) -> ShellExecutionResult:
        command = ["scp", *self._ssh(request)[1:], "--", source, destination]
        return self._execute(command, reported=["scp", source, destination])

    def _scp_host(self, request: VmRequest) -> str:
        host = self.connection_host(request)
        return f"{request.user}@{f'[{host}]' if ':' in host else host}"

    def transfer_to(
        self,
        request: VmRequest,
        *,
        source: Path,
        destination: str,
    ) -> ShellExecutionResult:
        """Upload a file without acquiring lifecycle ownership."""
        return self._transfer(
            request, str(source.resolve()), f"{self._scp_host(request)}:{destination}"
        )

    def transfer_from(
        self,
        request: VmRequest,
        *,
        source: str,
        destination: Path,
    ) -> ShellExecutionResult:
        """Download evidence using the same explicit SSH identity."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        return self._transfer(
            request, f"{self._scp_host(request)}:{source}", str(destination.resolve())
        )
