"""nanoFaaS P0/P1 and the function alone, deployed, probed and removed on Docker."""

import base64
import hashlib
import json
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from nanolab.comparison.profiles import EXPERIMENT_02_MODULES
from nanolab.tasks.experiment_02.platforms import (
    FIRST_RESPONSE,
    DirectReference,
    NanofaasPlatform,
    leftover_containers,
)
from nanolab.tasks.experiment_02.vm import Vm

from .fakes import RecordingProvider, Result, request

PAYLOAD = b'{"size":128,"text":"A"}\n'


def _png(size: int) -> bytes:
    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + b"IHDR"
        + header
        + struct.pack(">I", zlib.crc32(b"IHDR" + header))
    )


PNG = _png(128)
ENVELOPE = json.dumps(
    {
        "status": "success",
        "statusCode": 200,
        "encoding": "base64",
        "headers": {"content-type": "image/png"},
        "output": base64.b64encode(PNG).decode(),
    }
).encode()


def _p1() -> NanofaasPlatform:
    return NanofaasPlatform(
        profile="P1",
        control_plane_image="registry/control-plane:p1",
        function_image="registry/rust-qr-code:e02",
        modules=EXPERIMENT_02_MODULES["P1"],
        concurrency=2,
        queue_capacity=200,
        timeout_ms=30000,
    )


def _p0() -> NanofaasPlatform:
    return NanofaasPlatform(
        profile="P0",
        control_plane_image="registry/control-plane:p0",
        function_image="registry/rust-qr-code:e02",
        modules=EXPERIMENT_02_MODULES["P0"],
        concurrency=2,
        queue_capacity=200,
        timeout_ms=30000,
    )


def _docker_runs(provider: RecordingProvider) -> list[tuple[str, ...]]:
    return [argv for argv in provider.commands() if argv[:2] == ("docker", "run")]


def _deployed(platform) -> tuple[RecordingProvider, float]:
    def handler(argv, _env):
        if argv == ("date", "+%s.%N"):
            return Result(0, "100.0\n")
        return None

    provider = RecordingProvider(handler=handler)
    return provider, platform.deploy(Vm(provider, request()))


def test_p1_runs_the_docker_provider_and_registers_a_deployment_function():
    provider, started = _deployed(_p1())
    assert started == 100.0
    [control_plane] = _docker_runs(provider)
    assert "/var/run/docker.sock:/var/run/docker.sock" in control_plane
    assert "NANOFAAS_DEPLOYMENT_DEFAULT_BACKEND=container-local" in control_plane
    assert control_plane[-1] == "registry/control-plane:p1"
    assert _p1().registration() == {
        "name": "qr-code-rust",
        "image": "registry/rust-qr-code:e02",
        "executionMode": "DEPLOYMENT",
        "concurrency": 2,
        "queueSize": 200,
        "maxRetries": 0,
        "timeoutMs": 30000,
    }


def test_p0_registers_an_external_endpoint_without_the_docker_socket():
    provider, _started = _deployed(_p0())
    function, control_plane = _docker_runs(provider)
    assert function[-1] == "registry/rust-qr-code:e02"
    assert "PORT=8080" in function
    assert not any("docker.sock" in part for part in control_plane)
    assert _p0().registration() == {
        "name": "qr-code-rust",
        "image": "registry/rust-qr-code:e02",
        "executionMode": "EXTERNAL",
        "endpointUrl": "http://e02-qr-code:8080/invoke",
        "concurrency": 2,
        "maxRetries": 0,
        "timeoutMs": 30000,
    }


def test_images_are_present_before_the_clock_starts():
    provider, _started = _deployed(_p1())
    commands = provider.commands()
    clock = commands.index(("date", "+%s.%N"))
    pulls = [
        index
        for index, argv in enumerate(commands)
        if argv[0] == "sh" and "docker pull" in argv[-1]
    ]
    assert pulls
    assert max(pulls) < clock


def test_direct_reference_wraps_the_payload_as_runtime_input():
    direct = DirectReference("registry/rust-qr-code:e02")
    assert json.loads(direct.body(PAYLOAD)) == {"input": {"size": 128, "text": "A"}}
    target = direct.target("10.0.0.4")
    assert target.url == "http://10.0.0.4:9000/invoke"
    assert (target.extractor, target.field) == ("json-base64", None)
    provider, _started = _deployed(direct)
    [function] = _docker_runs(provider)
    assert "9000:8080" in function
    assert any(part.startswith("EXECUTION_ID=") for part in function)


def _probe(platform, output: str, first: bytes | None, tmp_path: Path):
    def handler(argv, _env):
        if argv[0] == "sh":
            if first is not None:
                provider.files[str(FIRST_RESPONSE)] = first
            return Result(0, output)
        return None

    provider = RecordingProvider(handler=handler)
    return platform.readiness(
        Vm(provider, request()),
        started=100.0,
        timeout_seconds=300,
        payload=PAYLOAD,
        evidence=tmp_path,
    )


def test_readiness_reports_seconds_from_the_deployment_start(tmp_path):
    timings = _probe(
        _p1(), "api 100.5\nregistered 101.0\nfirst 102.25\n", ENVELOPE, tmp_path
    )
    assert timings == {
        "apiReadySeconds": 0.5,
        "registeredSeconds": 1.0,
        "firstCorrectSeconds": 2.25,
    }
    recorded = json.loads((tmp_path / "readiness.json").read_text())
    assert recorded["pngSha256"] == hashlib.sha256(PNG).hexdigest()


@pytest.mark.parametrize(
    "platform", [_p1(), _p0(), DirectReference("registry/rust-qr-code:e02")]
)
def test_readiness_script_is_valid_shell(platform, tmp_path):
    provider_scripts: list[str] = []

    def handler(argv, _env):
        if argv[0] == "sh":
            provider_scripts.append(argv[-1])
            return Result(0, "timeout api\n")
        return None

    platform.readiness(
        Vm(RecordingProvider(handler=handler), request()),
        started=100.0,
        timeout_seconds=1,
        payload=PAYLOAD,
        evidence=tmp_path,
    )
    [script] = provider_scripts
    subprocess.run(("sh", "-n", "-c", script), check=True)


def test_readiness_timeout_is_none(tmp_path):
    assert _probe(_p1(), "api 100.5\ntimeout registered\n", None, tmp_path) is None


def test_readiness_refuses_an_incorrect_first_response(tmp_path):
    wrong = ENVELOPE.replace(b"iVBOR", b"AAAAA")
    timings = _probe(
        _p1(), "api 100.5\nregistered 101.0\nfirst 102.25\n", wrong, tmp_path
    )
    assert timings is None


def test_teardown_removes_every_container_but_the_registry():
    def handler(argv, _env):
        if argv[:3] == ("docker", "ps", "-a"):
            return Result(0, "e02-control-plane\nnanofaas-e2e-registry\nqr-code-1\n")
        return None

    provider = RecordingProvider(handler=handler)
    vm = Vm(provider, request())
    assert leftover_containers(vm) == ["e02-control-plane", "qr-code-1"]
    _p1().teardown(vm)
    assert ("docker", "rm", "-f", "e02-control-plane", "qr-code-1") in (
        provider.commands()
    )


def test_record_refuses_a_control_plane_with_unexpected_modules(tmp_path):
    def metadata(modules: list[str]):
        def handler(argv, _env):
            if argv[0] == "curl" and argv[-1].endswith("/modules/build-metadata"):
                return Result(
                    0, json.dumps({"modules": modules, "build": {"type": "jvm"}})
                )
            return None

        return RecordingProvider(handler=handler)

    with pytest.raises(ValueError, match="modules"):
        _p1().record(Vm(metadata(["build-metadata"]), request()), tmp_path)
    _p1().record(Vm(metadata(sorted(EXPERIMENT_02_MODULES["P1"])), request()), tmp_path)
    assert json.loads((tmp_path / "build-metadata.json").read_text())["modules"]
