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
  status=$(curl -sS --retry 5 --retry-all-errors --retry-delay 1 --max-time 30 \
    --request DELETE -u "admin:$(cat %SECRET%)" \
    -H 'Content-Type: application/json' \
    -d '{"functionName":"qr","namespace":"openfaas-fn"}' \
    --output /dev/null --write-out '%{http_code}' \
    http://127.0.0.1:8081/system/functions)
  # A completed DELETE may time out before its response; verify state below.
  case "$status" in
    200|202|204|404) ;;
    *) echo "faasd DELETE failed with HTTP $status" >&2; exit 1 ;;
  esac
fi
systemctl stop faasd faasd-provider
for namespace in openfaas openfaas-fn; do
  tasks=$(ctr -n "$namespace" tasks list -q)
  containers=$(ctr -n "$namespace" containers list -q)
  [ -z "$tasks$containers" ]
done
python3 - <<'CNI'
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

root = Path('/var/run/cni/openfaas-cni-bridge')
cache_root = Path('/var/lib/cni/results')
owners = {}

def require(condition, reason):
    if not condition:
        raise SystemExit('CNI leases remain; refusing another cell: ' + reason)

# faasd 0.19.7 recreates stopped qr tasks without CNI DEL. The network ID
# includes the old PID, so deleting the final task cannot release those IPs.
# All tasks/containers are already absent above. Validate every attachment
# before invoking DEL, never erase IPAM lease files to hide a failed cleanup.
for path in root.iterdir() if root.is_dir() else ():
    try:
        ipaddress.ip_address(path.name)
    except ValueError:
        continue
    fields = path.read_text().splitlines()
    require(len(fields) == 2 and fields[1] == 'eth1', 'unknown lease ' + path.name)
    owner = fields[0]
    require(re.fullmatch(r'qr-[1-9][0-9]*', owner), 'unknown owner ' + owner)
    owners.setdefault(owner, set()).add(path.name)

# A previous DEL may have released its IP before another plugin failed.
# Include saved attachments without a remaining lease so retries finish them.
for path in cache_root.glob('openfaas-cni-bridge-qr-*-eth1'):
    owner = path.name[len('openfaas-cni-bridge-'):-len('-eth1')]
    require(re.fullmatch(r'qr-[1-9][0-9]*', owner), 'unknown cache ' + path.name)
    owners.setdefault(owner, set())

plans = []
if owners:
    current = json.loads(Path('/etc/cni/net.d/10-openfaas.conflist').read_text())
    require(current.get('name') == 'openfaas-cni-bridge', 'unknown network')
    plugins = current.get('plugins', [])
    require([p.get('type') for p in plugins] == ['bridge', 'firewall'],
            'unknown plugins')
    require(plugins[0].get('bridge') == 'openfaas0'
            and plugins[0].get('ipam', {}).get('type') == 'host-local'
            and plugins[0]['ipam'].get('dataDir') == str(root.parent), 'unknown IPAM')
    for owner, addresses in sorted(owners.items()):
        pid = owner.removeprefix('qr-')
        require(not Path('/proc/' + pid + '/ns/net').exists(), 'PID still live: ' + pid)
        networks = [('openfaas-cni-bridge', 'eth1'), ('cni-loopback', 'lo')]
        for network, interface in networks:
            path = cache_root / (network + '-' + owner + '-' + interface)
            if network == 'cni-loopback' and not path.exists():
                continue
            require(path.is_file() and not path.is_symlink(),
                    'missing cache ' + path.name)
            saved = json.loads(path.read_text())
            require(saved.get('kind') == 'cniCacheV1'
                    and saved.get('containerId') == owner
                    and saved.get('networkName') == network
                    and saved.get('ifName') == interface,
                    'cache ownership ' + path.name)
            config = json.loads(base64.b64decode(saved['config'], validate=True))
            attached_ips = []
            if network == 'openfaas-cni-bridge':
                require(config == current, 'changed network configuration')
                interfaces = saved['result'].get('interfaces', [])
                for ip in saved['result'].get('ips', []):
                    index = ip.get('interface')
                    require(type(index) is int and 0 <= index < len(interfaces),
                            'invalid cached interface')
                    if interfaces[index].get('name') == interface:
                        address = ipaddress.ip_interface(ip['address'])
                        require(address.version == 4, 'unsupported non-IPv4 cache')
                        attached_ips.append(str(address))
                require(attached_ips, 'no cached addresses for ' + interface)
                cached_ips = {str(ipaddress.ip_interface(ip).ip) for ip in attached_ips}
                require(addresses <= cached_ips, 'lease differs from cached result')
            else:
                require(config.get('name') == network
                        and config.get('plugins') == [{'type': 'loopback'}],
                        'unknown loopback')
            plans.append((path, owner, interface, config,
                          saved['result'], attached_ips))

def ip_command(*args):
    command = subprocess.run(['ip', *args], capture_output=True, text=True, timeout=30)
    require(command.returncode == 0, 'temporary namespace: ' + command.stderr)

nat_chains = set()
if owners:
    nat = subprocess.run(['iptables-save', '-t', 'nat'], capture_output=True,
                         text=True, timeout=30)
    require(nat.returncode == 0, 'cannot inspect NAT state: ' + nat.stderr)
    nat_chains = {line.split()[0][1:] for line in nat.stdout.splitlines()
                  if line.startswith(':')}

for owner in sorted(owners):
    attachments = [plan for plan in plans if plan[1] == owner]
    name = 'nanolab-e02-cni-' + uuid4().hex
    namespace = Path('/var/run/netns') / name
    created = False
    try:
        ip_command('netns', 'add', name)
        created = True
        # bridge 0.9.1 skips NAT removal for an empty/missing netns. Give its
        # native DEL an isolated, DOWN dummy with only the original eth1 IPs.
        # It deletes that dummy and its own masquerade rules, not a live netns.
        ip_command('-n', name, 'link', 'add', 'eth1', 'type', 'dummy')
        for address in attachments[0][-1]:
            ip_command('-n', name, 'address', 'add', address, 'dev', 'eth1')
        for path, _, interface, config, result, _ in attachments:
            env = {**os.environ, 'CNI_COMMAND': 'DEL', 'CNI_CONTAINERID': owner,
                   'CNI_NETNS': str(namespace), 'CNI_IFNAME': interface,
                   'CNI_PATH': '/opt/cni/bin', 'CNI_ARGS': ''}
            for plugin in reversed(config['plugins']):
                payload = {**plugin, 'name': config['name'],
                           'cniVersion': config['cniVersion'], 'prevResult': result}
                if plugin['type'] == 'bridge' and payload.get('ipMasq'):
                    # Pinned CNI 0.9.1 utils.FormatChainName uses SHA-512 and
                    # 28 characters including CNI-. A reboot may leave the
                    # cache but no NAT chain; native DEL otherwise errors.
                    digest = hashlib.sha512((config['name'] + owner).encode())
                    chain = 'CNI-' + digest.hexdigest()[:24]
                    if chain not in nat_chains:
                        payload['ipMasq'] = False
                deleted = subprocess.run(['/opt/cni/bin/' + plugin['type']],
                                         input=json.dumps(payload), text=True, env=env,
                                         capture_output=True, timeout=30)
                require(deleted.returncode == 0,
                        'CNI DEL ' + plugin['type'] + ' failed for ' + owner + ': '
                        + (deleted.stderr or deleted.stdout))
    finally:
        if created:
            ip_command('netns', 'del', name)
            require(not namespace.exists(), 'temporary namespace still exists')

remaining = []
for path in root.iterdir() if root.is_dir() else ():
    try:
        ipaddress.ip_address(path.name)
        remaining.append(path.name)
    except ValueError:
        pass
require(not remaining, ', '.join(remaining))
# Keep every cache if any plugin failed or IPAM did not release its lease.
# Idempotent DEL can then be retried with the original attachment identity.
for path, *_ in plans:
    path.unlink()
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
