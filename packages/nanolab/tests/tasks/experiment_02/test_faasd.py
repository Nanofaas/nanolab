"""faasd uses containerd namespaces and ephemeral authentication."""

import json
import os
import subprocess
from pathlib import PurePosixPath

from nanolab.tasks.experiment_02.faasd import Faasd, teardown_argv
from nanolab.tasks.experiment_02.platforms import FIRST_RESPONSE

from .fakes import RecordingProvider, Result, vm
from .test_baselines_containers import PAYLOAD, PROBE, _protocol


def test_faasd_target_and_payload_preserve_the_shared_function_contract():
    platform = Faasd(root=PurePosixPath("/opt/faasd"), image="registry/qr:recorded")
    target = platform.target("faasd.internal")
    assert target.url == "http://faasd.internal:30080/function/qr"
    assert target.extractor == "json-base64"
    assert platform.body(b'{"input":{"text":"A"}}') == b'{"input":{"text":"A"}}'
    assert "faasd" in platform.host_commands


def test_faasd_cleanup_is_confined_to_its_containerd_namespaces():
    argv = teardown_argv()
    script = argv[-1]
    assert "docker" not in script
    assert "openfaas openfaas-fn" in script
    assert "systemctl stop faasd faasd-provider" in script
    assert script.index("--request DELETE") < script.index("systemctl stop")
    assert "tasks delete" not in script
    assert "CNI leases remain" in script


def test_faasd_start_does_not_touch_docker_or_persist_the_password(tmp_path):
    def handler(argv, _env):
        if argv[0] == "python3":
            provider.files[str(FIRST_RESPONSE)] = b'"..."'
            return Result(0, json.dumps(PROBE))
        return None

    provider = RecordingProvider(handler=handler)
    platform = Faasd(root=PurePosixPath("/var/lib/faasd"), image="registry/qr:recorded")
    timings = platform.start(
        vm(provider),
        protocol=_protocol(),
        payload=PAYLOAD,
        evidence=tmp_path,
    )
    assert timings is not None
    assert not any("docker" in argv for argv in provider.commands())
    spec = json.loads((tmp_path / "readiness-spec.json").read_text())
    assert spec["invoke"]["headersCommand"]
    assert "Authorization" not in spec["invoke"]["headers"]


def test_cleanup_refuses_an_unreachable_containerd(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, content in {
        "ctr": "exit 1",
        "systemctl": "exit 0",
    }.items():
        path = bindir / name
        path.write_text(f"#!/bin/sh\n{content}\n")
        path.chmod(0o755)
    result = subprocess.run(
        ("sh", "-c", teardown_argv()[-1]),
        env={**os.environ, "PATH": f"{bindir}:{os.defpath}"},
        capture_output=True,
    )
    assert result.returncode != 0
