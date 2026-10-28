"""The platforms experiment 02 deploys on the stack VM's Docker engine.

Every platform does the same five things for a cell: start and become ready,
say where requests go, record what actually ran, and leave. Here are nanoFaaS
P0 and P1 and the function alone; the baselines follow the same protocol.

Starting is one VM-side script, `assets/diagnostics/experiment02_readiness.py`:
it reads the clock, runs the platform's containers and polls every 0.5 s for
the API, the registration and the first correct response, so no SSH round trip
of the operator lands inside a timing. Images are pulled before the clock, so
readiness never includes a download. An HTTP 200 carrying the wrong image is
not readiness.

Teardown removes every container except the registry, including those a
platform created on its own, which no name here could list in advance.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from nanolab.comparison.prepare import (
    comparison_remote_command,
    comparison_transfer_from,
    comparison_transfer_to,
)
from nanolab.config.experiment_01 import invocation_body
from nanolab.config.experiment_02 import Experiment02Protocol
from nanolab.core.deployment import CONTROL_PLANE_NODE_PORT, REGISTRY_CONTAINER_NAME
from nanolab.tasks.experiment_02.budget import PLATFORM_UNIT
from nanolab.tasks.experiment_02.generator import K6Target
from nanolab.tasks.experiment_02.refusal import CellRefusedError
from nanolab.tasks.experiment_02.vm import Vm, exec_status
from nanolab.workspace.paths import bundled_assets_root

NETWORK = "nanolab-e02"
CONTROL_PLANE = "e02-control-plane"
FUNCTION_CONTAINER = "e02-qr-code"
FUNCTION_NAME = "qr-code-rust"
# The stack's only inbound ports the loadgen may reach: the NodePorts that
# `secure_release_endpoints` opens to the operator and the loadgen alone. Every
# platform publishes its entry point on them, so no new port is ever exposed.
ENTRY_PORT = CONTROL_PLANE_NODE_PORT
MANAGEMENT_PORT = 30081
READINESS_SCRIPT = bundled_assets_root() / "diagnostics" / "experiment02_readiness.py"
READINESS_INTERVAL_SECONDS = 0.5
# Relative, so under the VM user's home, where every remote command starts.
WORKDIR = PurePosixPath("nanolab-e02/readiness")
FIRST_RESPONSE = WORKDIR / "first-response"
_JSON = {"Content-Type": "application/json"}
_API = f"http://127.0.0.1:{ENTRY_PORT}"
_READY = f"http://127.0.0.1:{MANAGEMENT_PORT}/actuator/health/readiness"
_CALLBACK = f"http://{CONTROL_PLANE}:8080/v1/internal/executions"
# The runtime admits one active execution per id (sdks/rust/src/occupancy.rs):
# with no platform to number the requests, the generator sends each its own.
EXECUTION_ID_HEADER = "X-Execution-Id"


class Platform(Protocol):
    """What a cell needs from any platform it measures."""

    @property
    def metrics_url(self) -> str | None:
        """Return where the sampler scrapes the platform's metrics, if anywhere."""
        ...

    @property
    def host_commands(self) -> tuple[str, ...]:
        """Return the `comm` of the platform's processes that run outside Docker."""
        ...

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Start; return seconds to API, registration and first correct answer.

        `None` when the platform gave no correct answer before the protocol's
        readiness timeout; `evidence/readiness.json` says at which stage. A
        start command that fails raises: that is the engine, not the platform.
        """
        ...

    def target(self, host: str) -> K6Target:
        """Return where the generator sends requests and how it reads answers."""
        ...

    def secret_headers(self, vm: Vm) -> Mapping[str, str]:
        """Return headers requests need but no evidence may hold, after start."""
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
    names = comparison_remote_command(
        vm.provider, vm.request, ("docker", "ps", "-a", "--format", "{{.Names}}")
    ).split()
    return [name for name in names if name != REGISTRY_CONTAINER_NAME]


# A platform may still be removing its own containers, or creating them (Fn
# does both), when the teardown runs: `docker rm` refuses the first, misses the
# second, so it is repeated until nothing but the registry is left.
_REMOVAL_WAIT_SECONDS = 60.0
_REMOVAL_POLL_SECONDS = 2.0


class ContainersRemainError(RuntimeError):
    """Containers the teardown could not remove before its deadline."""

    def __init__(self, names: Sequence[str]) -> None:
        """Name the containers that remain."""
        super().__init__(f"containers remain after the teardown: {list(names)}")
        self.names = list(names)


def _remove_containers(vm: Vm) -> None:
    deadline = time.monotonic() + _REMOVAL_WAIT_SECONDS
    # Remove again on every pass: a platform may create containers meanwhile.
    while names := leftover_containers(vm):
        _ = exec_status(vm, ("docker", "rm", "-f", *names))
        if not leftover_containers(vm):
            return
        if time.monotonic() >= deadline:
            raise ContainersRemainError(names)
        time.sleep(_REMOVAL_POLL_SECONDS)


def baseline_teardown(vm: Vm) -> None:
    """Stop a baseline's host unit and compose project, then every container."""
    _ = exec_status(vm, ("sudo", "systemctl", "stop", PLATFORM_UNIT))
    _ = exec_status(vm, ("sudo", "docker", "compose", "-p", "e02", "down", "-v"))
    _remove_containers(vm)
    # tinyFaaS creates a network per function; left over, they exhaust the
    # engine's address pools for the next platform.
    _ = exec_status(vm, ("docker", "network", "prune", "-f"))


def record_containers(vm: Vm, evidence: Path) -> None:
    """Record `docker inspect` of every container but the registry, minus its env."""
    evidence.mkdir(parents=True, exist_ok=True)
    names = leftover_containers(vm)
    if names:
        inspected = json.loads(
            comparison_remote_command(
                vm.provider, vm.request, ("docker", "inspect", *names)
            )
        )
        # A container's environment may hold a platform's secret (FunLess's
        # SECRET_KEY_BASE signs its tokens): it is never evidence.
        for container in inspected:
            container.get("Config", {}).pop("Env", None)
        (evidence / "containers.json").write_text(json.dumps(inspected, indent=2))


def _ensure_images(vm: Vm, images: Sequence[str]) -> None:
    for image in images:
        quoted = shlex.quote(image)
        _ = comparison_remote_command(
            vm.provider,
            vm.request,
            (
                "sh",
                "-c",
                f"docker image inspect {quoted} >/dev/null 2>&1 "
                f"|| docker pull -q {quoted} >/dev/null",
            ),
        )


def _ensure_network(vm: Vm) -> None:
    if exec_status(vm, ("docker", "network", "inspect", NETWORK)).return_code:
        _ = comparison_remote_command(
            vm.provider, vm.request, ("docker", "network", "create", NETWORK)
        )


def start_platform(
    vm: Vm,
    *,
    images: Sequence[str],
    commands: Sequence[Sequence[str]],
    api: str,
    register: Mapping[str, object] | None,
    target: K6Target,
    body: bytes,
    protocol: Experiment02Protocol,
    payload: bytes,
    evidence: Path,
    api_any_status: bool = False,
    headers_command: Sequence[str] | None = None,
    docker_network: bool = True,
) -> dict[str, float] | None:
    """Pull and prepare, then start and time the platform in one VM-side run.

    `register` is the readiness spec's registration, HTTP or commands.
    `headers_command` prints headers the requests need but the evidence must
    not hold, such as a token; the VM-side probe runs it itself.
    """
    _ensure_images(vm, images)
    if docker_network:
        _ensure_network(vm)
    evidence.mkdir(parents=True, exist_ok=True)
    spec = {
        "start": [list(command) for command in commands],
        "api": api,
        "apiAnyStatus": api_any_status,
        "register": None if register is None else dict(register),
        "invoke": {
            "url": target.url,
            "headers": dict(target.headers),
            "body": body.decode("utf-8"),
            "extractor": target.extractor,
            "field": target.field,
            "idHeader": target.id_header,
            "size": json.loads(payload)["size"],
            "pngSha256": protocol.png_sha256,
            "timeoutSeconds": protocol.http_timeout_seconds,
            "headersCommand": None
            if headers_command is None
            else list(headers_command),
        },
        "timeoutSeconds": protocol.readiness_timeout_seconds,
        "intervalSeconds": READINESS_INTERVAL_SECONDS,
        "out": str(FIRST_RESPONSE),
    }
    local_spec = evidence / "readiness-spec.json"
    local_spec.write_text(json.dumps(spec, indent=2) + "\n")
    _ = comparison_remote_command(
        vm.provider,
        vm.request,
        ("sh", "-c", f"rm -rf {WORKDIR} && mkdir -p {WORKDIR}"),
    )
    remote_script = WORKDIR / READINESS_SCRIPT.name
    remote_spec = WORKDIR / "spec.json"
    comparison_transfer_to(vm.provider, vm.request, READINESS_SCRIPT, remote_script)
    comparison_transfer_to(vm.provider, vm.request, local_spec, remote_spec)
    probe = json.loads(
        comparison_remote_command(
            vm.provider,
            vm.request,
            ("python3", "-I", str(remote_script), str(remote_spec)),
        )
    )
    if probe.get("failed") == "start":
        # The engine or a busy port, not the platform: an error, never a result.
        (evidence / "readiness.json").write_text(json.dumps(probe, indent=2) + "\n")
        raise RuntimeError(
            f"start command failed ({probe.get('code')}): {probe.get('command')}"
        )
    started = float(probe["started"])

    def since(stage: str) -> float | None:
        return float(probe[stage]) - started if stage in probe else None

    timings = {
        "apiReadySeconds": since("api"),
        # Without a registration step the function is registered when it is up.
        "registeredSeconds": since("registered" if register else "api"),
        "firstCorrectSeconds": since("first"),
    }
    (evidence / "readiness.json").write_text(
        json.dumps(
            {
                **timings,
                "pngSha256": probe.get("pngSha256"),
                "intervalSeconds": READINESS_INTERVAL_SECONDS,
                "probe": probe,
            },
            indent=2,
        )
        + "\n"
    )
    if "first" not in probe:
        return None
    comparison_transfer_from(
        vm.provider, vm.request, FIRST_RESPONSE, evidence / "first-response.body"
    )
    return {name: value for name, value in timings.items() if value is not None}


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
    metrics_url: str | None = f"http://127.0.0.1:{MANAGEMENT_PORT}/actuator/prometheus"
    host_commands: tuple[str, ...] = ()

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

    def commands(self) -> list[tuple[str, ...]]:
        """Return the containers to start, in order: for P0 the function first."""
        ports = ("-p", f"{ENTRY_PORT}:8080", "-p", f"{MANAGEMENT_PORT}:8081")
        metrics = ("-e", "NANOFAAS_METRICS_PROFILE=advanced")
        if self.profile == "P0":
            return [
                (
                    "docker", "run", "-d", "--name", FUNCTION_CONTAINER,
                    "--network", NETWORK, "-e", "PORT=8080",
                    "-e", f"CALLBACK_URL={_CALLBACK}", self.function_image,
                ),
                (
                    "docker", "run", "-d", "--name", CONTROL_PLANE,
                    "--network", NETWORK, *ports, *metrics, self.control_plane_image,
                ),
            ]  # fmt: skip
        return [
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
        ]  # fmt: skip

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Start the control plane, register the function, wait for a correct PNG."""
        return start_platform(
            vm,
            images=(self.control_plane_image, self.function_image),
            commands=self.commands(),
            api=_READY,
            register={
                "url": f"{_API}/v1/functions",
                "check": f"{_API}/v1/functions/{FUNCTION_NAME}",
                "body": self.registration(),
            },
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
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

    def secret_headers(self, vm: Vm) -> Mapping[str, str]:
        """Return no secret: nanoFaaS's API needs none here."""
        return {}

    def body(self, payload: bytes) -> bytes:
        """Wrap the payload as the invocation request's `input`."""
        return invocation_body(payload)

    def record(self, vm: Vm, evidence: Path) -> None:
        """Record the containers and refuse a control plane with other modules."""
        record_containers(vm, evidence)
        metadata = comparison_remote_command(
            vm.provider, vm.request, ("curl", "-fsS", f"{_API}/modules/build-metadata")
        )
        (evidence / "build-metadata.json").write_text(metadata)
        data = json.loads(metadata)
        running = sorted(data.get("modules", []))
        if running != sorted(self.modules):
            raise CellRefusedError(
                f"running control plane has modules {running}, "
                f"expected {sorted(self.modules)}"
            )
        kind = data.get("build", {}).get("type")
        if self.runtime is not None and kind != self.runtime:
            raise CellRefusedError(
                f"running control plane is {kind}, expected {self.runtime}"
            )

    def teardown(self, vm: Vm) -> None:
        """Deregister the function and remove every container but the registry."""
        _ = exec_status(
            vm,
            (
                "curl", "-s", "-o", "/dev/null", "-m", "5", "-X", "DELETE",
                f"{_API}/v1/functions/{FUNCTION_NAME}",
            ),
        )  # fmt: skip
        _remove_containers(vm)
        _ = exec_status(vm, ("docker", "network", "rm", NETWORK))


@dataclass(frozen=True)
class DirectReference:
    """The function alone, invoked through its own runtime with no platform."""

    function_image: str
    metrics_url: str | None = None
    host_commands: tuple[str, ...] = ()

    def commands(self, *, concurrency: int) -> list[tuple[str, ...]]:
        """Return the one container to start, publishing the runtime's port.

        The runtime admits `concurrency` handlers, the protocol's fixed
        concurrency that nanoFaaS P1 admits too. Its default of 32 collapses
        under contention (paper notes/evidence.md, 27 October 2026): this is a
        comparison policy, not a fix of that contention.
        """
        return [
            (
                "docker", "run", "-d", "--name", FUNCTION_CONTAINER,
                "--network", NETWORK, "-p", f"{ENTRY_PORT}:8080",
                "-e", "PORT=8080",
                "-e", f"NANOFAAS_MAX_CONCURRENT_HANDLERS={concurrency}",
                self.function_image,
            )
        ]  # fmt: skip

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Start the function container and wait for a correct answer."""
        return start_platform(
            vm,
            images=(self.function_image,),
            commands=self.commands(concurrency=protocol.fixed_concurrency),
            api=f"{_API}/health",
            register=None,
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Read the runtime's answer: the PNG as a base64 JSON string."""
        return K6Target(
            f"http://{host}:{ENTRY_PORT}/invoke",
            _JSON,
            "json-base64",
            id_header=EXECUTION_ID_HEADER,
        )

    def secret_headers(self, vm: Vm) -> Mapping[str, str]:
        """Return no secret: the runtime needs none."""
        return {}

    def body(self, payload: bytes) -> bytes:
        """Wrap the payload as the runtime's invocation `input`."""
        return invocation_body(payload)

    def record(self, vm: Vm, evidence: Path) -> None:
        """Record the containers; there is no build to check."""
        record_containers(vm, evidence)

    def teardown(self, vm: Vm) -> None:
        """Remove every container but the registry."""
        _remove_containers(vm)
        _ = exec_status(vm, ("docker", "network", "rm", NETWORK))
