"""The baseline platforms of experiment 02 on the stack VM.

Each runs the paper's one qr-code implementation through its own wrapper
(`nanofaas-paper/functions/qr-code`), installed once per run under `OPT`, and
starts inside the budget: containers through Docker's `cgroup-parent`, host
processes in the platform unit (`budget.host_service`). Versions, contracts
and quirks are the portability check's of 27 October 2026
(`nanofaas-paper/notes/evidence.md`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from nanolab.comparison.prepare import comparison_remote_command
from nanolab.config.experiment_02 import Budget, Experiment02Protocol
from nanolab.tasks.experiment_02.budget import host_service
from nanolab.tasks.experiment_02.generator import K6Target
from nanolab.tasks.experiment_02.platforms import (
    ENTRY_PORT,
    _record_containers,
    _start,
    baseline_teardown,
)
from nanolab.tasks.experiment_02.vm import Vm

# Where the run installs platforms and wrappers on the stack; absolute, since
# host processes run as root from systemd.
OPT = PurePosixPath("/opt/nanolab-e02")
_LOCAL = f"http://127.0.0.1:{ENTRY_PORT}"
_JSON = {"Content-Type": "application/json"}


class _Baseline:
    """What every baseline shares: no metrics, no host process, no secret."""

    metrics_url: str | None = None
    host_commands: tuple[str, ...] = ()

    def secret_headers(self, vm: Vm) -> Mapping[str, str]:
        """Return no secret."""
        return {}

    def record(self, vm: Vm, evidence: Path) -> None:
        """Record the containers."""
        _record_containers(vm, evidence)

    def teardown(self, vm: Vm) -> None:
        """Stop the platform unit and compose project, then every container."""
        baseline_teardown(vm)


# FunLess seeds a guest token in its core container at every start.
_FUNLESS_TOKEN = "sudo docker exec e02-core-1 sed -n 's/^Guest=//p' /tmp/funless/tokens"
_FUNLESS_HEADERS = (
    "sh",
    "-c",
    f'printf \'{{"Authorization":"Bearer %s"}}\' "$({_FUNLESS_TOKEN})"',
)


@dataclass(frozen=True)
class FunLess(_Baseline):
    """FunLess 0.9.0: core, worker and Postgres in compose; the module is Wasm."""

    root: PurePosixPath = OPT / "funless"

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Bring the compose project up, create module and function, then probe."""
        bearer = f'-H "Authorization: Bearer $({_FUNLESS_TOKEN})"'
        return _start(
            vm,
            images=(),
            commands=[
                (
                    "sh",
                    "-c",
                    # A fresh signing secret per cell, generated on the VM.
                    'sudo env SECRET_KEY_BASE="$(head -c 48 /dev/urandom | base64 -w0)"'
                    f" docker compose -p e02 -f {self.root}/compose.yaml up -d",
                )
            ],
            api=f"{_LOCAL}/v1/",
            api_any_status=True,
            register={
                "commands": [
                    (
                        "sh",
                        "-c",
                        f"curl -fsS {bearer} -H 'Content-Type: application/json'"
                        f' -d \'{{"module":{{"name":"e02"}}}}\' {_LOCAL}/v1/fn',
                    ),
                    (
                        "sh",
                        "-c",
                        f"curl -fsS {bearer} -F name=qr"
                        f" -F code=@{self.root}/qr_funless.wasm {_LOCAL}/v1/fn/e02",
                    ),
                ]
            },
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
            headers_command=_FUNLESS_HEADERS,
        )

    def target(self, host: str) -> K6Target:
        """Invoke through the core; the PNG is in `data`."""
        return K6Target(
            f"http://{host}:{ENTRY_PORT}/v1/fn/e02/qr", _JSON, "json-base64", "data"
        )

    def body(self, payload: bytes) -> bytes:
        """Send the payload as the invocation's `args`."""
        return json.dumps({"args": json.loads(payload)}, separators=(",", ":")).encode()

    def secret_headers(self, vm: Vm) -> Mapping[str, str]:
        """Read this deployment's guest token, for the generator alone."""
        return json.loads(
            comparison_remote_command(vm.provider, vm.request, _FUNLESS_HEADERS)
        )


@dataclass(frozen=True)
class Spin(_Baseline):
    """Spin 4.2.5 serving the qr-code component; one host process."""

    root: PurePosixPath = OPT / "spin"
    host_commands: tuple[str, ...] = ("spin",)

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Run `spin up` in the platform unit and probe the component."""
        return _start(
            vm,
            images=(),
            commands=[
                host_service(
                    (
                        str(self.root / "spin"),
                        "up",
                        "--listen",
                        f"0.0.0.0:{ENTRY_PORT}",
                        "-f",
                        str(self.root / "spin.toml"),
                    )
                )
            ],
            # The component answers any request; a GET without a body is a 400.
            api=f"{_LOCAL}/",
            api_any_status=True,
            register=None,
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Read the component's answer: the PNG as a base64 JSON string."""
        return K6Target(f"http://{host}:{ENTRY_PORT}/", _JSON, "json-base64")

    def body(self, payload: bytes) -> bytes:
        """Send the payload itself."""
        return payload


@dataclass(frozen=True)
class TinyFaas(_Baseline):
    """tinyFaaS eff00d0: its manager on the host, the function in Docker.

    The `binary` runtime forks `fn.sh` per request; HTTP on the entry port,
    CoAP and gRPC off.
    """

    root: PurePosixPath = OPT / "tinyfaas"
    # `comm` keeps 15 characters of a process name.
    host_commands: tuple[str, ...] = ("tinyfaas-linux-", "rproxy-linux-am")

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Run the manager in the platform unit, upload the function, probe it."""
        upload = (
            "import json, sys; d = json.load(open(sys.argv[1]));"
            f' d["threads"] = {protocol.fixed_concurrency};'
            " sys.stdout.write(json.dumps(d))"
        )
        return _start(
            vm,
            images=(),
            commands=[
                host_service(
                    ("sh", "-c", f"cd {self.root} && exec ./tinyfaas-linux-amd64"),
                    env={
                        "http_PORT": str(ENTRY_PORT),
                        "coap_PORT": "-1",
                        "grpc_PORT": "-1",
                    },
                )
            ],
            api="http://127.0.0.1:8080/list",
            api_any_status=True,
            register={
                "commands": [
                    (
                        "sh",
                        "-c",
                        f"python3 -c '{upload}' {self.root}/upload.json"
                        " | curl -fsS --data-binary @- http://127.0.0.1:8080/upload",
                    )
                ]
            },
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Read the function's stdout: the PNG as a base64 JSON string."""
        return K6Target(f"http://{host}:{ENTRY_PORT}/qr", _JSON, "json-base64")

    def body(self, payload: bytes) -> bytes:
        """Send the payload itself, the function's stdin."""
        return payload


ETCD = "gcr.io/etcd-development/etcd:v3.5.13"
SERVERLEDGE_IMAGE = "127.0.0.1:5000/qr-serverledge:e02"


@dataclass(frozen=True)
class Serverledge(_Baseline):
    """Serverledge 45419f5 with etcd; the function is a `custom` image.

    The container pool gets the cell's budget, so the platform admits no more
    than the budget holds.
    """

    budget: Budget
    root: PurePosixPath = OPT / "serverledge"
    host_commands: tuple[str, ...] = ("serverledge",)

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Start etcd and Serverledge, register the function, probe it."""
        conf = self.root / "serverledge-conf.yaml"
        settings = (
            f"api:\\n  port: {ENTRY_PORT}\\ncontainer:\\n  pool:\\n"
            f"    memory: {self.budget.memory_mib}\\n    cpus: {self.budget.cpus:g}\\n"
        )
        return _start(
            vm,
            images=(ETCD, SERVERLEDGE_IMAGE),
            commands=[
                ("sudo", "sh", "-c", f"printf '{settings}' > {conf}"),
                (
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    "e02-etcd",
                    "-p",
                    "127.0.0.1:2379:2379",
                    ETCD,
                    "/usr/local/bin/etcd",
                    "--listen-client-urls",
                    "http://0.0.0.0:2379",
                    "--advertise-client-urls",
                    "http://0.0.0.0:2379",
                ),
                host_service((str(self.root / "bin" / "serverledge"), str(conf))),
            ],
            api=f"{_LOCAL}/status",
            api_any_status=True,
            register={
                "commands": [
                    (
                        str(self.root / "bin" / "serverledge-cli"),
                        "-P",
                        str(ENTRY_PORT),
                        "create",
                        "--function",
                        "qr",
                        "--memory",
                        "512",
                        "--runtime",
                        "custom",
                        "--custom_image",
                        SERVERLEDGE_IMAGE,
                        "--max_concurrency",
                        str(protocol.fixed_concurrency),
                    )
                ]
            },
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Read the PNG from `Result`, a JSON string holding its base64."""
        return K6Target(
            f"http://{host}:{ENTRY_PORT}/invoke/qr",
            _JSON,
            "json-string-base64",
            "Result",
        )

    def body(self, payload: bytes) -> bytes:
        """Send the payload as the invocation's `Params`."""
        return json.dumps(
            {"Params": json.loads(payload)}, separators=(",", ":")
        ).encode()


FNSERVER = (
    "fnproject/fnserver@sha256:"
    "16dedb58b2f1be5e02d7a851a66041ea97076801cbdb09cdfee0100296a68686"
)
FN_IMAGE = "127.0.0.1:5000/qr-fn:e02"
# Idempotent: a retried registration finds what an earlier attempt created.
_FN_REGISTER = f"""
import json, urllib.request
api = "{_LOCAL}/v2"
def call(path, body=None):
    request = urllib.request.Request(api + path, method="POST" if body else "GET",
        data=json.dumps(body).encode() if body else None,
        headers={{"Content-Type": "application/json"}})
    return json.load(urllib.request.urlopen(request, timeout=30))
def ensure(kind, query, body):
    found = call(f"/{{kind}}?{{query}}")["items"]
    return found[0]["id"] if found else call(f"/{{kind}}", body)["id"]
app = ensure("apps", "name=e02", {{"name": "e02"}})
fn = ensure("fns", f"app_id={{app}}&name=qr", {{"name": "qr", "app_id": app,
    "image": "{FN_IMAGE}", "memory": 256, "timeout": TIMEOUT}})
ensure("triggers", f"app_id={{app}}&name=qr", {{"name": "qr", "app_id": app,
    "fn_id": fn, "type": "http", "source": "/qr"}})
"""


@dataclass(frozen=True)
class Fn(_Baseline):
    """Fn 0.3.770 in single-server mode, starting function containers itself."""

    root: PurePosixPath = OPT / "fn"

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Start fnserver, create app, function and trigger, probe the trigger."""
        iofs = self.root / "iofs"
        register = _FN_REGISTER.replace("TIMEOUT", str(protocol.http_timeout_seconds))
        return _start(
            vm,
            images=(FNSERVER, FN_IMAGE),
            commands=[
                (
                    "docker",
                    "run",
                    "-d",
                    "--privileged",
                    "--name",
                    "e02-fnserver",
                    "-v",
                    f"{iofs}:/iofs",
                    "-e",
                    f"FN_IOFS_DOCKER_PATH={iofs}",
                    "-e",
                    "FN_IOFS_PATH=/iofs",
                    "-v",
                    "/var/run/docker.sock:/var/run/docker.sock",
                    "-p",
                    f"{ENTRY_PORT}:8080",
                    FNSERVER,
                )
            ],
            api=f"{_LOCAL}/version",
            register={"commands": [("python3", "-c", register)]},
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
        )

    def target(self, host: str) -> K6Target:
        """Invoke through the HTTP trigger; the PNG is a base64 JSON string."""
        return K6Target(f"http://{host}:{ENTRY_PORT}/t/e02/qr", _JSON, "json-base64")

    def body(self, payload: bytes) -> bytes:
        """Send the payload itself."""
        return payload
