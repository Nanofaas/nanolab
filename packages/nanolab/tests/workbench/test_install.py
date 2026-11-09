import json
import subprocess
from typing import Any, cast

import pytest
import yaml

from nanolab.tasks.infra.ansible import bundled_ansible_root
from nanolab.workbench import install

INPUTS = cast("Any", None)  # InstallWorkbench ignores its inputs


def test_requirements_pin_the_tag():
    data = yaml.safe_load((bundled_ansible_root() / "requirements.yml").read_text())
    (entry,) = data["collections"]
    assert entry["name"] == "git+https://github.com/Nanofaas/nanofaas-workbench.git"
    assert entry["type"] == "git" or entry["name"].startswith("git+")
    assert entry["version"] == "v0.1.0"


def _manifest(path, version):
    folder = path / "ansible_collections/nanofaas/workbench"
    folder.mkdir(parents=True)
    (folder / "MANIFEST.json").write_text(
        json.dumps({"collection_info": {"version": version}})
    )


def test_install_skipped_when_version_present(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(tmp_path))
    _manifest(tmp_path / ".ansible/collections", "0.1.0")
    monkeypatch.setattr(
        install.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    outcome = install.InstallWorkbench().run(INPUTS)
    assert outcome.value == tmp_path / ".ansible/collections"


def test_install_runs_galaxy_with_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(tmp_path))
    seen = []
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda argv, **k: (
            seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        ),
    )
    outcome = install.InstallWorkbench().run(INPUTS)
    (argv,) = seen
    assert argv[:3] == ["ansible-galaxy", "collection", "install"]
    assert argv[argv.index("-r") + 1].endswith("ansible_assets/requirements.yml")
    assert argv[argv.index("-p") + 1] == str(outcome.value)


def test_install_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "boom"),
    )
    with pytest.raises(RuntimeError, match="boom"):
        install.InstallWorkbench().run(INPUTS)
