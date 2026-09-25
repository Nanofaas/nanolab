from __future__ import annotations

from pathlib import Path

from sonata_tasks.shell import RecordingShell

from nanolab.tasks.infra.ansible import AnsibleAdapter, bundled_ansible_root
from nanolab.tasks.vm.models import VmRequest


def test_provision_base_uses_bundled_ansible_root() -> None:
    shell = RecordingShell()
    adapter = AnsibleAdapter(repo_root=Path("/repo"), shell=shell)
    request = VmRequest(lifecycle="external", host="vm.example.test", user="dev")

    adapter.provision_base(request, dry_run=True)

    command = shell.commands[0]
    assert "ansible-playbook" in command
    assert "infra/ansible_assets/playbooks/provision-base.yml" in " ".join(command)
    assert "vm.example.test," in command


def test_bundled_ansible_assets_exist_on_disk() -> None:
    adapter = AnsibleAdapter(repo_root=Path("/repo"))
    assert (adapter.ansible_root / "playbooks" / "provision-base.yml").is_file()
    assert (adapter.ansible_root / "ansible.cfg").is_file()


def test_ansible_root_override_is_respected(tmp_path: Path) -> None:
    adapter = AnsibleAdapter(repo_root=Path("/repo"), ansible_root=tmp_path)
    assert adapter.ansible_root == tmp_path


def test_configure_k3s_registry_sets_expected_extra_vars() -> None:
    shell = RecordingShell()
    adapter = AnsibleAdapter(repo_root=Path("/repo"), shell=shell)
    request = VmRequest(lifecycle="external", host="vm.example.test", user="dev")

    adapter.configure_k3s_registry(
        request, registry="registry.example.test:5000", dry_run=True
    )

    rendered = " ".join(shell.commands[0])
    assert "configure-k3s-registry.yml" in rendered
    assert "registry=registry.example.test:5000" in rendered
    assert "registry_port=5000" in rendered


def test_provision_k3s_sets_expected_extra_vars() -> None:
    shell = RecordingShell()
    adapter = AnsibleAdapter(repo_root=Path("/repo"), shell=shell)
    request = VmRequest(lifecycle="external", host="vm.example.test", user="dev")

    adapter.provision_k3s(
        request, kubeconfig_path="/home/dev/.kube/config", dry_run=True
    )

    rendered = " ".join(shell.commands[0])
    assert "provision-k3s.yml" in rendered
    assert "kubeconfig_path=/home/dev/.kube/config" in rendered


def test_ensure_registry_container_sets_expected_extra_vars() -> None:
    shell = RecordingShell()
    adapter = AnsibleAdapter(repo_root=Path("/repo"), shell=shell)
    request = VmRequest(lifecycle="external", host="vm.example.test", user="dev")

    adapter.ensure_registry_container(request, registry="reg.test:5000", dry_run=True)

    rendered = " ".join(shell.commands[0])
    assert "ensure-registry.yml" in rendered
    assert "registry_container_name=nanofaas-e2e-registry" in rendered
    assert "registry_host=reg.test" in rendered


def test_provision_k3s_resolves_the_release_dynamically() -> None:
    playbook = (bundled_ansible_root() / "playbooks" / "provision-k3s.yml").read_text(
        encoding="utf-8"
    )

    assert "https://api.github.com/repos/k3s-io/k3s/releases/latest" in playbook
    assert "k3s_version_override" in playbook
    assert "tag_name" in playbook


def test_provision_base_and_ensure_registry_preserve_idempotence_guards() -> None:
    base = (bundled_ansible_root() / "playbooks" / "provision-base.yml").read_text(
        encoding="utf-8"
    )
    registry = (bundled_ansible_root() / "playbooks" / "ensure-registry.yml").read_text(
        encoding="utf-8"
    )

    assert '("v" ~ helm_version)' in base
    assert "docker port {{ registry_container_name }} 5000/tcp" in registry


def test_provision_base_installs_uv() -> None:
    base = (bundled_ansible_root() / "playbooks" / "provision-base.yml").read_text(
        encoding="utf-8"
    )

    assert "Install uv" in base
    assert "UV_INSTALL_DIR" in base or "command -v uv" in base


def test_provision_base_installs_the_project_jdk() -> None:
    base = (bundled_ansible_root() / "playbooks" / "provision-base.yml").read_text(
        encoding="utf-8"
    )

    assert "openjdk-25-jdk-headless" in base
