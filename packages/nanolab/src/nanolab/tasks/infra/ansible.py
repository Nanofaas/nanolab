"""Run the bundled Ansible playbooks against a VM, on the host or remotely."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from multipass_vm_sdk import MultipassClient
from sonata_tasks.ansible import build_ansible_argv
from sonata_tasks.shell import (
    ShellBackend,
    ShellExecutionResult,
    SubprocessShell,
)
from sonata_tasks.vm.models import VmRequest

from nanolab.tasks.deployment import REGISTRY_CONTAINER_NAME


class HostResolver(Protocol):
    """Resolve the SSH host a playbook should target for `request`."""

    def __call__(self, request: VmRequest, *, dry_run: bool = False) -> str:
        """Return the address to put in the inventory for `request`."""
        ...


def bundled_ansible_root() -> Path:
    """Path to the Ansible playbooks bundled inside the library."""
    return Path(__file__).parent / "ansible_assets"


class AnsibleAdapter:
    """Run bundled playbooks against a VM, with the SSH details filled in."""

    def __init__(
        self,
        repo_root: Path,
        shell: ShellBackend | None = None,
        host_resolver: HostResolver | None = None,
        private_key_path: Path | None = None,
        multipass_client: MultipassClient | None = None,
        ansible_root: Path | None = None,
    ) -> None:
        """Wire the adapter to its shell, host resolver, key and playbook root.

        `host_resolver` stays multipass's by default, built from
        `multipass_client` and consulted once per inventory target.
        """
        self.repo_root = Path(repo_root)
        # Playbooks are bundled with the library; callers may override.
        self.ansible_root = (
            Path(ansible_root) if ansible_root is not None else bundled_ansible_root()
        )
        self.shell = shell or SubprocessShell()
        if host_resolver is None:
            from sonata_tasks.vm.providers.multipass import resolve_connection_host

            client = multipass_client or MultipassClient()

            def multipass_host_resolver(
                request: VmRequest, dry_run: bool = False
            ) -> str:
                return resolve_connection_host(request, client, dry_run=dry_run)

            host_resolver = multipass_host_resolver

        self.host_resolver = host_resolver
        self.private_key_path = private_key_path

    def _inventory_target(self, request: VmRequest, *, dry_run: bool = False) -> str:
        return f"{self.host_resolver(request, dry_run=dry_run)},"

    def _build_command(
        self,
        playbook_name: str,
        request: VmRequest,
        *,
        extra_vars: dict[str, str] | None = None,
        dry_run: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        playbook = self.ansible_root / "playbooks" / playbook_name
        command = list(
            build_ansible_argv(
                playbook=playbook,
                inventory=self._inventory_target(request, dry_run=dry_run),
                user=request.user,
                private_key_path=self.private_key_path,
                extra_vars=extra_vars,
            )
        )
        env = {"ANSIBLE_CONFIG": str(self.ansible_root / "ansible.cfg")}
        return command, env

    def run_playbook(
        self,
        playbook_name: str,
        request: VmRequest,
        *,
        extra_vars: dict[str, str] | None = None,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Run `playbook_name` against `request` and return the shell result."""
        command, env = self._build_command(
            playbook_name, request, extra_vars=extra_vars, dry_run=dry_run
        )
        return self.shell.run(command, cwd=self.repo_root, env=env, dry_run=dry_run)

    def _registry_extra_vars(
        self,
        *,
        registry: str,
        container_name: str | None = None,
    ) -> dict[str, str]:
        registry_host, registry_port = registry.rsplit(":", 1)
        extra_vars = {
            "registry": registry,
            "registry_host": registry_host,
            "registry_port": registry_port,
        }
        if container_name is not None:
            extra_vars["registry_container_name"] = container_name
        return extra_vars

    def provision_base(
        self,
        request: VmRequest,
        *,
        install_helm: bool = False,
        helm_version: str = "3.16.4",
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Install the base VM dependencies, Helm included when asked."""
        return self.run_playbook(
            "provision-base.yml",
            request,
            extra_vars={
                "install_helm": str(install_helm).lower(),
                "helm_version": helm_version.removeprefix("v"),
                "vm_user": request.user,
            },
            dry_run=dry_run,
        )

    def provision_release_builder(
        self,
        request: VmRequest,
        *,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Install release-only image transport tools on the stack VM."""
        return self.run_playbook(
            "provision-release-builder.yml",
            request,
            extra_vars={"vm_user": request.user},
            dry_run=dry_run,
        )

    def provision_k3s(
        self,
        request: VmRequest,
        *,
        kubeconfig_path: str,
        k3s_version: str | None = None,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Install k3s and write the kubeconfig at `kubeconfig_path`."""
        extra_vars = {
            "vm_user": request.user,
            "kubeconfig_path": kubeconfig_path,
        }
        if k3s_version:
            extra_vars["k3s_version_override"] = k3s_version
        return self.run_playbook(
            "provision-k3s.yml",
            request,
            extra_vars=extra_vars,
            dry_run=dry_run,
        )

    def ensure_registry_container(
        self,
        request: VmRequest,
        *,
        registry: str,
        container_name: str = REGISTRY_CONTAINER_NAME,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Ensure the registry container runs on the VM."""
        return self.run_playbook(
            "ensure-registry.yml",
            request,
            extra_vars=self._registry_extra_vars(
                registry=registry,
                container_name=container_name,
            ),
            dry_run=dry_run,
        )

    def configure_k3s_registry(
        self,
        request: VmRequest,
        *,
        registry: str,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Point k3s at `registry` so it can pull the images it is given."""
        return self.run_playbook(
            "configure-k3s-registry.yml",
            request,
            extra_vars=self._registry_extra_vars(registry=registry),
            dry_run=dry_run,
        )

    def configure_registry(
        self,
        request: VmRequest,
        *,
        registry: str,
        container_name: str = REGISTRY_CONTAINER_NAME,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Ensure the registry container, then point k3s at the registry."""
        ensure_result = self.ensure_registry_container(
            request,
            registry=registry,
            container_name=container_name,
            dry_run=dry_run,
        )
        if ensure_result.return_code != 0:
            return ensure_result
        return self.configure_k3s_registry(
            request,
            registry=registry,
            dry_run=dry_run,
        )


@dataclass
class RunPlaybook:
    """Honest Task that runs an ansible playbook on the host via AnsibleAdapter.

    Connectivity is parametric through the injected adapter (host_resolver +
    private_key_path) and extra_vars (e.g. ansible_port for non-22 SSH).
    Satisfies the sonata_tasks.Task protocol; raises on non-zero exit so
    Workflow.run() stops and triggers cleanup.
    """

    task_id: str
    title: str
    adapter: AnsibleAdapter
    playbook: str
    request: VmRequest
    extra_vars: dict[str, str] | None = None

    def run(self) -> None:
        """Run the playbook and raise its output if the exit code was non-zero."""
        result = self.adapter.run_playbook(
            self.playbook, self.request, extra_vars=self.extra_vars
        )
        if result.return_code != 0:
            # Surface stdout AND stderr: ansible reports task failures on stdout
            # (PLAY RECAP / "fatal: ... FAILED!") while benign warnings go to stderr,
            # so stderr alone would mask the real error.
            detail = "\n".join(
                part for part in (result.stdout.strip(), result.stderr.strip()) if part
            )
            raise RuntimeError(
                detail or f"{self.task_id} failed (exit {result.return_code})"
            )


def install_k6_task(
    *,
    task_id: str,
    title: str,
    repo_root: Path,
    shell: ShellBackend,
    host: str,
    user: str,
    private_key: Path | None = None,
    port: int | None = None,
) -> RunPlaybook:
    """Build a RunPlaybook for the k6 install against a resolved VM endpoint.

    The single, shared way to install k6 via ansible. Per-lifecycle connectivity
    is captured as plain arguments:
      - multipass: host=<resolved IP>, default user, multipass key, port=None
      - proxmox:   host=<proxmox host>, port=<published SSH port>, proxmox key
      - azure:     host=<public IP>, azure key, port=None
    """
    adapter = AnsibleAdapter(
        repo_root=repo_root,
        # AnsibleAdapter's ShellBackend argument, not the subprocess shell flag
        # bandit's B604 looks for.
        shell=shell,  # nosec B604
        host_resolver=lambda request, dry_run=False: host,
        private_key_path=private_key,
    )
    request = VmRequest(lifecycle="external", host=host, user=user)
    extra_vars = {"ansible_port": str(port)} if port is not None else None
    return RunPlaybook(
        task_id=task_id,
        title=title,
        adapter=adapter,
        playbook="install-k6.yml",
        request=request,
        extra_vars=extra_vars,
    )
