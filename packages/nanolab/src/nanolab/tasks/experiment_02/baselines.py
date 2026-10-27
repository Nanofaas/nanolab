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
from nanolab.config.experiment_02 import Experiment02Protocol
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
