"""faasd's containerd-only platform contract; no Docker commands or saved secrets."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from nanolab.comparison.prepare import comparison_remote_command
from nanolab.config.experiment_02 import Experiment02Protocol
from nanolab.tasks.experiment_02.budget import process_listing_argv
from nanolab.tasks.experiment_02.generator import K6Target
from nanolab.tasks.experiment_02.platforms import ENTRY_PORT, start_platform
from nanolab.tasks.experiment_02.vm import Vm

FAASD_ROOT = PurePosixPath("/var/lib/faasd")


def teardown_argv(
    root: PurePosixPath = FAASD_ROOT,
) -> tuple[str, ...]:
    """Use faasd's CNI-aware deletion and refuse to continue after cleanup failures."""
    secret = shlex.quote(str(root / "secrets/basic-auth-password"))
    script = r"""set -eu
containers=$(ctr -n openfaas-fn containers list -q)
if printf '%s\n' "$containers" | grep -qx qr; then
  systemctl start faasd-provider
  curl -fsS --retry 5 --retry-all-errors --retry-delay 1 --max-time 2 \
    --request DELETE -u "admin:$(cat %SECRET%)" \
    -H 'Content-Type: application/json' \
    -d '{"functionName":"qr","namespace":"openfaas-fn"}' \
    http://127.0.0.1:8081/system/functions >/dev/null
fi
systemctl stop faasd faasd-provider
for namespace in openfaas openfaas-fn; do
  tasks=$(ctr -n "$namespace" tasks list -q)
  containers=$(ctr -n "$namespace" containers list -q)
  [ -z "$tasks$containers" ]
done
python3 - <<'CNI'
import ipaddress
from pathlib import Path
root = Path('/var/run/cni/openfaas-cni-bridge')
leases = []
for path in root.iterdir() if root.is_dir() else ():
    try:
        ipaddress.ip_address(path.name)
    except ValueError:
        continue
    leases.append(path.name)
if leases:
    raise SystemExit('CNI leases remain; refusing another cell: ' + ', '.join(leases))
CNI
"""
    return ("sudo", "sh", "-c", script.replace("%SECRET%", secret))


@dataclass(frozen=True)
class Faasd:
    """Classic watchdog on the dedicated faasd VM, paired with P1."""

    root: PurePosixPath
    image: str
    metrics_url: str | None = None
    host_commands: tuple[str, ...] = (
        "faasd",
        "gateway",
        "worker",
        "nats-streaming-",
        "prometheus",
        "fwatchdog",
        "qr-stdio",
    )

    def headers_command(self) -> tuple[str, ...]:
        """Read the credential on the VM; the spec contains only this command."""
        password = shlex.quote(str(self.root / "secrets/basic-auth-password"))
        return (
            "sh",
            "-c",
            (
                'printf \'{"Authorization":"Basic %s"}\' '
                "\"$(printf 'admin:%s' \"$(sudo cat " + password + ')" | base64 -w0)"'
            ),
        )

    def start(
        self,
        vm: Vm,
        *,
        protocol: Experiment02Protocol,
        payload: bytes,
        evidence: Path,
    ) -> dict[str, float] | None:
        """Start already-installed services and register the prepared QR image."""
        password = shlex.quote(str(self.root / "secrets/basic-auth-password"))
        deployment = shlex.quote(json.dumps({"service": "qr", "image": self.image}))
        return start_platform(
            vm,
            images=(),
            commands=[("sudo", "systemctl", "start", "faasd-provider", "faasd")],
            api=f"http://127.0.0.1:{ENTRY_PORT}/healthz",
            api_any_status=True,
            register={
                "commands": [
                    (
                        "sh",
                        "-c",
                        (
                            f'curl -fsS -u "admin:$(sudo cat {password})" '
                            f"-H 'Content-Type: application/json' -d {deployment} "
                            f"http://127.0.0.1:{ENTRY_PORT}/system/functions"
                        ),
                    )
                ]
            },
            target=self.target("127.0.0.1"),
            body=self.body(payload),
            protocol=protocol,
            payload=payload,
            evidence=evidence,
            headers_command=self.headers_command(),
            docker_network=False,
        )

    def target(self, host: str) -> K6Target:
        """Read the shared stdio wrapper's JSON string from the watchdog."""
        return K6Target(
            f"http://{host}:{ENTRY_PORT}/function/qr",
            {"Content-Type": "application/json"},
            "json-base64",
        )

    def body(self, payload: bytes) -> bytes:
        """Preserve the shared request bytes."""
        return payload

    def secret_headers(self, vm: Vm) -> Mapping[str, str]:
        """Return authentication in memory for the generator, never evidence."""
        return json.loads(
            comparison_remote_command(
                vm.provider,
                vm.request,
                self.headers_command(),
            )
        )

    def record(self, vm: Vm, evidence: Path) -> None:
        """Record task PIDs and cgroups without container environment variables."""
        evidence.mkdir(parents=True, exist_ok=True)
        for namespace in ("openfaas", "openfaas-fn"):
            listing = comparison_remote_command(
                vm.provider,
                vm.request,
                ("sudo", "ctr", "-n", namespace, "tasks", "list"),
            )
            (evidence / f"{namespace}-tasks.txt").write_text(listing)
        (evidence / "processes.txt").write_text(
            comparison_remote_command(
                vm.provider,
                vm.request,
                process_listing_argv(),
            )
        )

    def teardown(self, vm: Vm) -> None:
        """Remove only the namespaces owned by this dedicated deployment."""
        comparison_remote_command(vm.provider, vm.request, teardown_argv(self.root))
