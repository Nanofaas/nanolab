"""Pinned native toolchains are prepared on the build host, never the checkout."""

import os
import shlex
import subprocess
from pathlib import PurePosixPath

import pytest

from nanolab.tasks.recipes.native_host import GRAALVM_HOME, native_host_command


def _source(tmp_path):
    source = tmp_path / "source with spaces"
    (source / "scripts").mkdir(parents=True)
    (source / "gradle.properties").write_text(
        "graalvmVersion=25.2.4\ngraalvmJavaVersion=25.0.4\n"
    )
    return source


def test_native_command_uses_captured_source_pins_and_safe_path(tmp_path):
    source = _source(tmp_path)
    remote = PurePosixPath("/home/operator/source with spaces")
    command = native_host_command(source, remote)
    assert command[:4] == ("sudo", "sh", "-eu", "-c")
    assert command[-3:] == (str(remote), "25.2.4", "25.0.4")
    assert str(source) not in command[4]
    assert not (source / "build").exists()


def test_native_command_requires_explicit_source_pins(tmp_path):
    source = _source(tmp_path)
    (source / "gradle.properties").write_text("javaVersion=25\n")
    with pytest.raises(ValueError, match=r"graalvmVersion.*graalvmJavaVersion"):
        native_host_command(source, PurePosixPath("/source"))


def test_native_bootstrap_installs_once_and_rejects_another_toolchain(tmp_path):
    source = _source(tmp_path)
    home = tmp_path / "graal"
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "calls"
    for name in ("apt-get", "dpkg-query", "gcc", "sha256sum"):
        tool = tools / name
        tool.write_text(f'#!/bin/sh\necho {name} "$*" >> {shlex.quote(str(calls))}\n')
        tool.chmod(0o755)
    uname = tools / "uname"
    uname.write_text("#!/bin/sh\necho x86_64\n")
    uname.chmod(0o755)
    installer = source / "scripts/install-graalvm.sh"
    installer.write_text(
        "set -eu\n"
        f'echo "install $*" >> {shlex.quote(str(calls))}\n'
        f"mkdir -p {shlex.quote(str(home / 'bin'))}\n"
        "printf '#!/bin/sh\\necho native-image 25.0.4\\n"
        "echo GraalVM Runtime Environment GraalVM CE 25.2.4+7.1\\n"
        "echo Substrate VM GraalVM CE 25.2.4+7\\n' > "
        f"{shlex.quote(str(home / 'bin/native-image'))}\n"
        f"chmod +x {shlex.quote(str(home / 'bin/native-image'))}\n"
    )
    command = native_host_command(source, PurePosixPath(str(source)))
    # Substitute the protected host directory only; execute the real script logic.
    script = command[4].replace(GRAALVM_HOME, str(home))
    env = {**os.environ, "PATH": f"{tools}:/usr/bin:/bin"}
    argv = ("sh", "-eu", "-c", script, *command[5:])
    for _ in range(2):
        result = subprocess.run(argv, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    log = calls.read_text()
    assert log.count("install amd64 25.2.4 25.0.4 community") == 1
    assert "install -y -q build-essential zlib1g-dev" in log
    (home / "bin/native-image").write_text("#!/bin/sh\necho wrong-version\n")
    rejected = subprocess.run(argv, env=env, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "toolchain" in rejected.stderr
