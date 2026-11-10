"""Runtime lifecycle can be resumed from persisted connection facts."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.ansible import AnsiblePlaybookTask


def test_runtime_cleanup_uses_saved_inventory_without_live_vm(tmp_path, monkeypatch):
    from nanolab.one_shot.runtime import run_runtime

    seen = []
    monkeypatch.setattr(AnsiblePlaybookTask, "run", lambda task, _: seen.append(task))
    monkeypatch.setattr(
        "nanolab.one_shot.runtime.ensure_installed", lambda: Path("/coll")
    )
    node = SimpleNamespace(
        config=SimpleNamespace(id="edge"),
        provider=SimpleNamespace(shell=None),
        ansible_vars=lambda _: {"ansible_host": "10.0.0.5", "ansible_user": "ubuntu"},
    )
    inputs = TaskInputs._for_resource_values({}, ())
    run_runtime(
        cast(Any, node),
        tmp_path,
        "deploy",
        inputs,
        vm=cast(Any, object()),
        variables={"runtime_command": ["--flag=a b"]},
    )
    node.ansible_vars = lambda _: pytest.fail("cleanup must use saved facts")
    run_runtime(cast(Any, node), tmp_path, "undeploy", inputs)
    assert len(seen) == 2
    task = seen[-1]
    inventory = Path(task.argv[task.argv.index("-i") + 1])
    assert (
        json.loads(inventory.read_text())["all"]["hosts"]["edge"]["ansible_host"]
        == "10.0.0.5"
    )
    assert json.loads((tmp_path / "ansible/edge-runtime-deploy.json").read_text())[
        "runtime_command"
    ] == ["--flag=a b"]


def test_cleanup_before_deploy_does_not_touch_infrastructure(tmp_path, monkeypatch):
    from nanolab.one_shot.runtime import run_runtime

    monkeypatch.setattr(
        AnsiblePlaybookTask, "run", lambda *_: pytest.fail("nothing was deployed")
    )
    node = SimpleNamespace(config=SimpleNamespace(id="edge"))
    run_runtime(
        cast(Any, node), tmp_path, "undeploy", TaskInputs._for_resource_values({}, ())
    )


def test_runtime_playbook_checks_identity_before_starting():
    import yaml

    from nanolab.tasks.infra.ansible import bundled_ansible_root

    path = bundled_ansible_root() / "playbooks/one-shot-runtime.yml"
    assert path.is_file()
    tasks = yaml.safe_load(path.read_text())[0]["tasks"]
    deploy = next(
        task["block"] for task in tasks if task["name"] == "Deploy the runtime"
    )
    names = [task["name"] for task in deploy]
    assert names.index("Verify loaded image identities") < names.index(
        "Start the control plane"
    )
    assert names.index("Verify archive checksum") < names.index("Load runtime images")


@pytest.mark.skipif(
    __import__("os").environ.get("NANOLAB_RUNTIME_INTEGRATION") != "1",
    reason="real Linux Ansible/Docker runtime smoke is opt-in",
)
def test_real_runtime_deploy_redeploy_reject_wrong_image_and_cleanup(tmp_path):
    import hashlib
    import os
    import subprocess
    import sys
    import uuid

    from nanolab.tasks.infra.ansible import bundled_ansible_root
    from nanolab.workbench.install import ensure_installed

    name = "nl-runtime-test-" + uuid.uuid4().hex[:10]
    image = (
        "busybox@sha256:"
        "a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec"
    )
    subprocess.run(
        ["docker", "pull", image], check=True, capture_output=True, timeout=120
    )
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format={{.Id}}", image], text=True
    ).strip()
    archive = tmp_path / "images.tar"
    subprocess.run(
        ["docker", "save", "-o", str(archive), image_id], check=True, timeout=60
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "all": {
                    "hosts": {
                        "localhost": {
                            "ansible_connection": "local",
                            "ansible_python_interpreter": sys.executable,
                        }
                    }
                }
            }
        )
    )
    values = {
        "runtime_name": name,
        "runtime_owner": str(tmp_path),
        "runtime_log_file": str(tmp_path / "runtime.log"),
        "runtime_archive": str(archive),
        "runtime_remote_archive": str(tmp_path / "received.tar"),
        "runtime_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "runtime_architecture": None,
        "runtime_images": [{"reference": image_id, "ids": [image_id]}],
        "runtime_control_image": image_id,
        "runtime_command": ["sh", "-c", "echo runtime-smoke; sleep 120"],
        "runtime_env": {},
        "runtime_peer_dir": None,
        "runtime_peer_file": None,
        "runtime_logs": True,
    }
    collections = ensure_installed()

    def run(action, **changes):
        variables = tmp_path / f"{action}.json"
        variables.write_text(
            json.dumps({**values, "runtime_action": action, **changes})
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ansible.cli.playbook",
                "-i",
                str(inventory),
                "-e",
                f"runtime_vars={variables}",
                str(bundled_ansible_root() / "playbooks/one-shot-runtime.yml"),
            ],
            env={**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(collections)},
            capture_output=True,
            text=True,
            timeout=180,
        )
        (tmp_path / f"{action}-{len(list(tmp_path.glob('*.log')))}.log").write_text(
            result.stdout + result.stderr
        )
        return result

    def inspect():
        return json.loads(
            subprocess.check_output(["docker", "inspect", name], text=True)
        )[0]

    try:
        wrong_hash = run("deploy", runtime_archive_sha256="0" * 64)
        assert wrong_hash.returncode != 0 and "checksum mismatch" in wrong_hash.stdout
        wrong_image = run(
            "deploy",
            runtime_images=[{"reference": image_id, "ids": ["sha256:" + "0" * 64]}],
        )
        assert wrong_image.returncode != 0 and "digest mismatch" in wrong_image.stdout
        assert (
            subprocess.run(["docker", "inspect", name], capture_output=True).returncode
            != 0
        )
        deployed = run("deploy")
        assert deployed.returncode == 0, deployed.stdout + deployed.stderr
        first = inspect()
        assert first["State"]["Running"] is True
        assert first["Config"]["Labels"]["nanolab.run"] == str(tmp_path)
        redeployed = run("deploy")
        assert redeployed.returncode == 0, redeployed.stdout + redeployed.stderr
        assert inspect()["Id"] == first["Id"]
        removed = run("undeploy")
        assert removed.returncode == 0, removed.stdout + removed.stderr
        assert "runtime-smoke" in (tmp_path / "runtime.log").read_text()
        assert (
            subprocess.run(["docker", "inspect", name], capture_output=True).returncode
            != 0
        )
        again = run("undeploy")
        assert again.returncode == 0, again.stdout + again.stderr
        assert "runtime-smoke" in (tmp_path / "runtime.log").read_text()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
