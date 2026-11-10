from __future__ import annotations

from nanolab.tasks.infra.ansible import bundled_ansible_root

_ANSIBLE_ROOT = bundled_ansible_root()


def test_provision_base_uses_ansible_facts_namespace_for_architecture() -> None:
    playbook = _ANSIBLE_ROOT / "playbooks" / "provision-base.yml"

    content = playbook.read_text(encoding="utf-8")

    assert "ansible_architecture" not in content
    assert 'ansible_facts["architecture"]' in content


def test_ansible_config_disables_deprecation_warnings() -> None:
    config = _ANSIBLE_ROOT / "ansible.cfg"

    content = config.read_text(encoding="utf-8")

    assert "deprecation_warnings = False" in content


def test_ansible_config_preserves_ssh_connection_multiplexing() -> None:
    config = _ANSIBLE_ROOT / "ansible.cfg"

    content = config.read_text(encoding="utf-8")

    assert "ControlMaster=auto" in content
    assert "ControlPersist=60s" in content


def test_provision_base_refreshes_ssh_after_docker_group_change() -> None:
    import yaml

    playbook = _ANSIBLE_ROOT / "playbooks" / "provision-base.yml"
    tasks = yaml.safe_load(playbook.read_text())[0]["tasks"]
    group_index = next(
        i
        for i, task in enumerate(tasks)
        if task.get("ansible.builtin.user", {}).get("groups") == "docker"
    )
    assert tasks[group_index + 1].get("ansible.builtin.meta") == "reset_connection"


def test_runtime_packages_do_not_pull_in_a_build_toolchain():
    import yaml

    document = yaml.safe_load(
        (_ANSIBLE_ROOT / "playbooks/provision-base.yml").read_text()
    )[0]
    tasks = document["tasks"]
    common = next(
        task["ansible.builtin.apt"]["name"]
        for task in tasks
        if task["name"] == "Install base packages"
    )
    assert {"docker.io", "python3-requests"} <= set(common)
    assert not {"openjdk-25-jdk-headless", "docker-buildx", "skopeo"} & set(common)
    build = next(task for task in tasks if task["name"] == "Install guest build tools")
    assert build["when"] == "install_build_tools | bool"
    assert set(build["ansible.builtin.apt"]["name"]) == {
        "openjdk-25-jdk-headless",
        "docker-buildx",
        "skopeo",
    }
