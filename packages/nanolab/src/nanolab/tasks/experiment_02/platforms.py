"""The platforms experiment 02 deploys on the stack VM's Docker engine.

Every platform does the same five things for a cell: start its containers,
become ready, say where requests go, record what actually ran, and leave. Here
are nanoFaaS P0 and P1 and the function alone; the baselines follow the same
protocol.

Readiness is measured on the VM, from the instant the first container is
started to the first correct response, with the API and the registration timed
on the way. Images are pulled before that instant, so readiness never includes
a download. The first response is fetched and checked in full: an HTTP 200
carrying the wrong image is not readiness.

Teardown removes every container except the registry, including those a
platform created on its own, which no name here could list in advance.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from nanolab.tasks.experiment_02.budget import REGISTRY_CONTAINER
from nanolab.tasks.experiment_02.generator import K6Target, png_in_response
from nanolab.tasks.experiment_02.vm import Vm

NETWORK = "nanolab-e02"
CONTROL_PLANE = "e02-control-plane"
FUNCTION_CONTAINER = "e02-qr-code"
FUNCTION_NAME = "qr-code-rust"
# The stack's only inbound ports the loadgen may reach: the NodePorts that
# `secure_release_endpoints` opens to the operator and the loadgen alone. Every
# platform publishes its entry point on them, so no new port is ever exposed.
ENTRY_PORT = 30080
MANAGEMENT_PORT = 30081
# Relative, so under the VM user's home, where every remote command starts.
WORKDIR = PurePosixPath("nanolab-e02/readiness")
FIRST_RESPONSE = WORKDIR / "first-response"
_JSON = {"Content-Type": "application/json"}
_API = f"http://127.0.0.1:{ENTRY_PORT}"
_READY = f"http://127.0.0.1:{MANAGEMENT_PORT}/actuator/health/readiness"
_CALLBACK = f"http://{CONTROL_PLANE}:8080/v1/internal/executions"


class Platform(Protocol):
    """What a cell needs from any platform it measures."""

    def deploy(self, vm: Vm) -> float:
        """Start the platform; return the VM time its first container started."""
        ...

    def readiness(
        self,
        vm: Vm,
        *,
        started: float,
        timeout_seconds: int,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Return seconds to API, registration and first correct response, or `None`."""
        ...

    def target(self, host: str) -> K6Target:
        """Return where the generator sends requests and how it reads answers."""
        ...

    def body(self, payload: bytes) -> bytes:
        """Return the exact bytes this platform is sent for the protocol's payload."""
        ...

    def record(self, vm: Vm, evidence: Path) -> None:
        """Record what is running, refusing a deployment other than the one asked."""
        ...

    def teardown(self, vm: Vm) -> None:
        """Remove everything the platform started."""
        ...


def leftover_containers(vm: Vm) -> list[str]:
    """Return every container on the VM except the registry."""
    names = vm.run(("docker", "ps", "-a", "--format", "{{.Names}}")).split()
    return [name for name in names if name != REGISTRY_CONTAINER]


def _remove_containers(vm: Vm) -> None:
    names = leftover_containers(vm)
    if names:
        _ = vm.run(("docker", "rm", "-f", *names))


def _record_containers(vm: Vm, evidence: Path) -> None:
    evidence.mkdir(parents=True, exist_ok=True)
    names = leftover_containers(vm)
    if names:
        inspected = vm.run(("docker", "inspect", *names))
        (evidence / "containers.json").write_text(inspected)


def _ensure_images(vm: Vm, images: Sequence[str]) -> None:
    for image in images:
        quoted = shlex.quote(image)
        _ = vm.run(
            (
                "sh",
                "-c",
                f"docker image inspect {quoted} >/dev/null 2>&1 "
                f"|| docker pull -q {quoted} >/dev/null",
            )
        )


def _ensure_network(vm: Vm) -> None:
    if vm.probe(("docker", "network", "inspect", NETWORK))[0]:
        _ = vm.run(("docker", "network", "create", NETWORK))


def _clock(vm: Vm) -> float:
    return float(vm.run(("date", "+%s.%N")).strip())


def _wait_script(steps: Sequence[tuple[str, str]], timeout_seconds: int) -> str:
    """Return a script that waits for each check in turn and prints when it passed."""
    lines = [f"deadline=$(( $(date +%s) + {timeout_seconds} ))"]
    for name, check in steps:
        lines += [
            f"until {check}; do",
            '  if [ "$(date +%s)" -ge "$deadline" ]; then',
            f'    echo "timeout {name}"; exit 0',
            "  fi",
            "  sleep 0.5",
            "done",
            f'echo "{name} $(date +%s.%N)"',
        ]
    return "\n".join(lines)


def _probe_readiness(
    vm: Vm,
    *,
    health_url: str,
    registration: dict[str, object] | None,
    invoke_url: str,
    body: bytes,
    target: K6Target,
    started: float,
    timeout_seconds: int,
    payload: bytes,
    evidence: Path,
) -> dict[str, float] | None:
    """Wait on the VM for API, registration and a first 200; then check the PNG."""
    body_file = WORKDIR / "body.json"
    spec_file = WORKDIR / "spec.json"
    setup = [
        f"rm -rf {WORKDIR} && mkdir -p {WORKDIR}",
        f"printf '%s' {shlex.quote(body.decode())} > {body_file}",
    ]
    steps = [
        ("api", shlex.join(("curl", "-fsS", "-m", "2", "-o", "/dev/null", health_url)))
    ]
    if registration is not None:
        setup.append(
            f"printf '%s' {shlex.quote(json.dumps(registration))} > {spec_file}"
        )
        steps.append(
            (
                "registered",
                shlex.join(
                    (
                        "curl", "-fsS", "-m", "10", "-o", "/dev/null", "-X", "POST",
                        f"{_API}/v1/functions", "-H", "Content-Type: application/json",
                        "--data-binary", f"@{spec_file}",
                    )
                ),
            )
        )  # fmt: skip
    invoke = shlex.join(
        (
            "curl", "-sS", "-m", "30", "-o", str(FIRST_RESPONSE), "-w", "%{http_code}",
            "-X", "POST", invoke_url, "-H", "Content-Type: application/json",
            "--data-binary", f"@{body_file}",
        )
    )  # fmt: skip
    steps.append(("first", f'[ "$({invoke})" = 200 ]'))
    output = vm.run(
        ("sh", "-c", "\n".join((*setup, _wait_script(steps, timeout_seconds))))
    )
    stamps: dict[str, float] = {}
    for line in output.splitlines():
        name, _, value = line.partition(" ")
        if name == "timeout":
            return None
        stamps[name] = float(value)
    evidence.mkdir(parents=True, exist_ok=True)
    first = evidence / "first-response.body"
    if not vm.get(FIRST_RESPONSE, first):
        return None
    png = png_in_response(target, first.read_bytes(), size=json.loads(payload)["size"])
    timings = {
        "apiReadySeconds": stamps["api"] - started,
        "registeredSeconds": stamps.get("registered", stamps["api"]) - started,
        "firstCorrectSeconds": stamps["first"] - started,
    }
    (evidence / "readiness.json").write_text(
        json.dumps(
            {
                **timings,
                "pngSha256": hashlib.sha256(png).hexdigest() if png else None,
            },
            indent=2,
        )
        + "\n"
    )
    return timings if png is not None else None


@dataclass(frozen=True)
class NanofaasPlatform:
    """nanoFaaS P0, before an external endpoint, or P1, with the Docker provider."""

    profile: Literal["P0", "P1"]
    control_plane_image: str
    function_image: str
    modules: tuple[str, ...]
    concurrency: int
    queue_capacity: int
    timeout_ms: int
    runtime: Literal["jvm", "native"] | None = None

    def registration(self) -> dict[str, object]:
        """Return the function registration this composition receives."""
        spec: dict[str, object] = {"name": FUNCTION_NAME, "image": self.function_image}
        if self.profile == "P1":
            spec |= {
                "executionMode": "DEPLOYMENT",
                "concurrency": self.concurrency,
                "queueSize": self.queue_capacity,
            }
        else:
            spec |= {
                "executionMode": "EXTERNAL",
                "endpointUrl": f"http://{FUNCTION_CONTAINER}:8080/invoke",
                "concurrency": self.concurrency,
            }
        return spec | {"maxRetries": 0, "timeoutMs": self.timeout_ms}

    def deploy(self, vm: Vm) -> float:
        """Start the control plane, and for P0 the function it fronts."""
        _ensure_images(vm, (self.control_plane_image, self.function_image))
        _ensure_network(vm)
        started = _clock(vm)
        ports = ("-p", f"{ENTRY_PORT}:8080", "-p", f"{MANAGEMENT_PORT}:8081")
        metrics = ("-e", "NANOFAAS_METRICS_PROFILE=advanced")
        if self.profile == "P0":
            _ = vm.run(
                (
                    "docker", "run", "-d", "--name", FUNCTION_CONTAINER,
                    "--network", NETWORK, "-e", "PORT=8080",
                    "-e", f"CALLBACK_URL={_CALLBACK}", self.function_image,
                )
            )  # fmt: skip
            _ = vm.run(
                (
                    "docker", "run", "-d", "--name", CONTROL_PLANE,
                    "--network", NETWORK, *ports, *metrics, self.control_plane_image,
                )
            )  # fmt: skip
            return started
        _ = vm.run(
            (
                "docker", "run", "-d", "--name", CONTROL_PLANE, "--user", "0:0",
                "--network", NETWORK, *ports,
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-e", "NANOFAAS_DEPLOYMENT_DEFAULT_BACKEND=container-local",
                "-e", "NANOFAAS_CONTAINER_LOCAL_RUNTIME_ADAPTER=docker-java",
                "-e", f"NANOFAAS_CONTAINER_LOCAL_NETWORK_NAME={NETWORK}",
                "-e", f"NANOFAAS_CONTAINER_LOCAL_CALLBACK_URL={_CALLBACK}",
                *metrics, self.control_plane_image,
            )
        )  # fmt: skip
        return started

    def readiness(
        self,
        vm: Vm,
        *,
        started: float,
        timeout_seconds: int,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Wait for the API, register the function, and wait for a correct answer."""
        return _probe_readiness(
            vm,
            health_url=_READY,
            registration=self.registration(),
            invoke_url=f"{_API}/v1/functions/{FUNCTION_NAME}:invoke",
            body=self.body(payload),
            target=self.target("127.0.0.1"),
            started=started,
            timeout_seconds=timeout_seconds,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Send requests to the control plane's invocation API."""
        return K6Target(
            f"http://{host}:{ENTRY_PORT}/v1/functions/{FUNCTION_NAME}:invoke",
            _JSON,
            "nanofaas-envelope",
        )

    def body(self, payload: bytes) -> bytes:
        """Send the control plane the payload itself."""
        return payload

    def record(self, vm: Vm, evidence: Path) -> None:
        """Record the containers and refuse a control plane with other modules."""
        _record_containers(vm, evidence)
        metadata = vm.run(("curl", "-fsS", f"{_API}/modules/build-metadata"))
        (evidence / "build-metadata.json").write_text(metadata)
        data = json.loads(metadata)
        running = sorted(data.get("modules", []))
        if running != sorted(self.modules):
            raise ValueError(
                f"running control plane has modules {running}, "
                f"expected {sorted(self.modules)}"
            )
        kind = data.get("build", {}).get("type")
        if self.runtime is not None and kind != self.runtime:
            raise ValueError(
                f"running control plane is {kind}, expected {self.runtime}"
            )

    def teardown(self, vm: Vm) -> None:
        """Deregister the function and remove every container but the registry."""
        _ = vm.probe(
            (
                "curl", "-s", "-o", "/dev/null", "-m", "5", "-X", "DELETE",
                f"{_API}/v1/functions/{FUNCTION_NAME}",
            )
        )  # fmt: skip
        _remove_containers(vm)
        _ = vm.probe(("docker", "network", "rm", NETWORK))


@dataclass(frozen=True)
class DirectReference:
    """The function alone, invoked through its own runtime with no platform."""

    function_image: str

    def deploy(self, vm: Vm) -> float:
        """Start the function container, publishing its runtime port."""
        _ensure_images(vm, (self.function_image,))
        _ensure_network(vm)
        started = _clock(vm)
        _ = vm.run(
            (
                "docker", "run", "-d", "--name", FUNCTION_CONTAINER,
                "--network", NETWORK, "-p", f"{ENTRY_PORT}:8080",
                "-e", "PORT=8080",
                # The runtime needs an execution id; with no platform to send
                # one per request, every request uses this one.
                "-e", "EXECUTION_ID=e02-direct-reference",
                self.function_image,
            )
        )  # fmt: skip
        return started

    def readiness(
        self,
        vm: Vm,
        *,
        started: float,
        timeout_seconds: int,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Wait for the runtime's health, then for a correct answer."""
        return _probe_readiness(
            vm,
            health_url=f"{_API}/health",
            registration=None,
            invoke_url=f"{_API}/invoke",
            body=self.body(payload),
            target=self.target("127.0.0.1"),
            started=started,
            timeout_seconds=timeout_seconds,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Read the runtime's answer: the PNG as a base64 JSON string."""
        return K6Target(f"http://{host}:{ENTRY_PORT}/invoke", _JSON, "json-base64")

    def body(self, payload: bytes) -> bytes:
        """Wrap the payload as the runtime's invocation `input`."""
        return json.dumps(
            {"input": json.loads(payload)}, separators=(",", ":")
        ).encode()

    def record(self, vm: Vm, evidence: Path) -> None:
        """Record the containers; there is no build to check."""
        _record_containers(vm, evidence)

    def teardown(self, vm: Vm) -> None:
        """Remove every container but the registry."""
        _remove_containers(vm)
        _ = vm.probe(("docker", "network", "rm", NETWORK))
