import json
import re
import subprocess
import sys
from pathlib import Path
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
    assert entry["type"] == "git"
    assert entry["version"] == "v0.2.0"


def _manifest(path, version):
    folder = path / "ansible_collections/nanofaas/workbench"
    folder.mkdir(parents=True)
    (folder / "MANIFEST.json").write_text(
        json.dumps({"collection_info": {"version": version}})
    )


def _galaxy(seen):
    """Record each argv and install the pinned version, as galaxy would."""

    def run(argv, **_kwargs):
        seen.append(argv)
        target = Path(argv[argv.index("-p") + 1])
        manifest = target / "ansible_collections/nanofaas/workbench/MANIFEST.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"collection_info": {"version": "0.2.0"}}))
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


@pytest.fixture
def ansible_ok(monkeypatch):
    monkeypatch.setattr(install, "_ansible_python", lambda: "/usr/bin/python3")


def test_install_refuses_the_wrong_ansible_before_galaxy(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(tmp_path))
    seen = []
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda argv, **k: (
            seen.append(argv[0])
            or subprocess.CompletedProcess(argv, 0, "ansible [core 2.16.3]\n", "")
        ),
    )
    with pytest.raises(RuntimeError, match=r"needs ansible-core 2\.20"):
        install.InstallWorkbench().run(INPUTS)
    assert "ansible-galaxy" not in seen


def test_install_skipped_when_version_present(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(tmp_path))
    _manifest(tmp_path / ".ansible/collections", "0.2.0")
    monkeypatch.setattr(
        install.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    outcome = install.InstallWorkbench().run(INPUTS)
    assert outcome.value == tmp_path / ".ansible/collections"


def test_install_runs_galaxy_with_pin(tmp_path, monkeypatch, ansible_ok):
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(tmp_path))
    seen = []
    monkeypatch.setattr(
        install.subprocess,
        "run",
        _galaxy(seen),
    )
    outcome = install.InstallWorkbench().run(INPUTS)
    (argv,) = seen
    assert argv[:5] == [
        sys.executable,
        "-m",
        "ansible.cli.galaxy",
        "collection",
        "install",
    ]
    assert argv[argv.index("-r") + 1].endswith("ansible_assets/requirements.yml")
    assert argv[argv.index("-p") + 1] == str(outcome.value)


def test_install_failure_raises(tmp_path, monkeypatch, ansible_ok):
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "boom"),
    )
    with pytest.raises(RuntimeError, match="boom"):
        install.InstallWorkbench().run(INPUTS)


def test_install_that_installs_nothing_raises(tmp_path, monkeypatch, ansible_ok):
    """Galaxy exits 0 when requires_ansible rules every candidate out."""
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, "conflict", ""),
    )
    with pytest.raises(RuntimeError, match="conflict"):
        install.InstallWorkbench().run(INPUTS)


def test_install_forces_over_a_stale_version(tmp_path, monkeypatch, ansible_ok):
    monkeypatch.setenv("NANOLAB_WORKSPACE", str(tmp_path))
    _manifest(tmp_path / ".ansible/collections", "0.0.9")
    seen = []
    monkeypatch.setattr(
        install.subprocess,
        "run",
        _galaxy(seen),
    )
    install.InstallWorkbench().run(INPUTS)
    assert "--force" in seen[0]


ANSIBLE = (
    "ansible [core 2.20.1]\n"
    "  config file = None\n"
    "  python version = 3.12.3 (main) [GCC 13.2.0] (/opt/ansible/bin/python3)\n"
)


def _operator(monkeypatch, *, version=ANSIBLE, requests=0, docker=0):
    """Answer the preflight's commands; record every argv."""
    seen = []

    def run(argv, **_kwargs):
        seen.append(tuple(argv))
        if "ansible.cli.adhoc" in argv:
            return subprocess.CompletedProcess(argv, 0, version, "")
        code = requests if argv[0] == sys.executable else docker
        return subprocess.CompletedProcess(argv, code, "", "boom")

    monkeypatch.setattr(install.subprocess, "run", run)
    return seen


def test_operator_preflight_passes(monkeypatch):
    seen = _operator(monkeypatch)
    install.check_operator([("127.0.0.1", 0)])
    assert seen == [
        (sys.executable, "-m", "ansible.cli.adhoc", "--version"),
        (sys.executable, "-c", "import requests"),
        ("docker", "info"),
    ]


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({"version": ANSIBLE.replace("2.20.1", "2.19.4")}, r"ansible-core 2\.20"),
        ({"version": "nonsense"}, r"ansible-core 2\.20"),
        ({"requests": 1}, "requests"),
        ({"docker": 1}, "docker info"),
    ],
)
def test_operator_preflight_refuses(monkeypatch, answers, message):
    _operator(monkeypatch, **answers)
    with pytest.raises(RuntimeError, match=message):
        install.check_operator([])


def test_operator_preflight_refuses_a_bound_port(monkeypatch):
    import socket

    _operator(monkeypatch)
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        with pytest.raises(
            RuntimeError, match=re.escape(f"127.0.0.1:{port} is already bound")
        ):
            install.check_operator([("127.0.0.1", port)])


def test_a_missing_ansible_is_refused(monkeypatch):
    def run(argv, **_kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(install.subprocess, "run", run)
    with pytest.raises(RuntimeError, match=r"ansible-core 2\.20"):
        install.check_operator([])


def test_an_address_not_up_yet_is_not_a_bound_port(monkeypatch):
    _operator(monkeypatch)
    install.check_operator([("192.0.2.1", 3100)])  # TEST-NET: never local


def test_preflight_failure_prevents_resource_acquisition(monkeypatch):
    from sonata_engine import Workflow

    from nanolab.tasks.resource_tasks import call, resource_of

    acquired = []
    vm = resource_of("VM", lambda _: acquired.append("vm"), lambda *_: None)
    monkeypatch.setattr(
        install,
        "check_operator",
        lambda _: (_ for _ in ()).throw(RuntimeError("Docker unavailable")),
    )
    workflow = Workflow("preflight")
    workflow.add(install.OperatorPreflight())
    workflow.add(call("Use VM", lambda _: None), requires=(vm,))
    with pytest.raises(RuntimeError, match="Docker unavailable"):
        workflow.run()
    assert acquired == []


def test_preflight_uses_the_operator_python_instead_of_path(monkeypatch):
    import sys

    seen = []

    def run(*argv):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, ANSIBLE, "")

    monkeypatch.setattr(install, "_run", run)
    assert install._ansible_python() == sys.executable
    assert seen == [(sys.executable, "-m", "ansible.cli.adhoc", "--version")]


def test_preflight_passes_platform_ports_before_install(monkeypatch):
    seen = []
    monkeypatch.setattr(
        install, "check_operator", lambda ports: seen.append(tuple(ports))
    )
    monkeypatch.setattr(
        install, "ensure_installed", lambda: seen.append("install") or Path("/coll")
    )
    install.OperatorPreflight((("127.0.0.1", 9090),)).run(INPUTS)
    assert seen == [(("127.0.0.1", 9090),), "install"]
