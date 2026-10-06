from __future__ import annotations

from nanolab.tasks.infra.ansible import bundled_ansible_root


def test_bundled_ansible_assets_exist_on_disk() -> None:
    root = bundled_ansible_root()
    assert (root / "playbooks" / "provision-base.yml").is_file()
    assert (root / "ansible.cfg").is_file()


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
