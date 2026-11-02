"""Multipass VMs on a remote host, driven from the operator over SSH.

The local one-shot path drives the Multipass daemon on the operator. A remote
site reuses the same provider: lifecycle commands (`launch`, `info`, `delete`)
run the site's Multipass client over SSH, and commands and file transfers to a
VM use the provider's own SSH, jumped through the site because the VM sits
behind the site's NAT. One shared SSH connection per site keeps the many short
commands of a run from paying a handshake each.

A site whose only reachable port is SSH (`tunnel`) has its VMs' forwarded ports
published on an operator address instead, by an operator-owned `ssh -L` per
node that carries each port to the same port on the site's loopback.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import signal
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from multipass_vm_sdk import CommandResult, MultipassClient

from nanolab.config.environment import MultipassSite
from nanolab.tasks.vm.orchestrator import VmOrchestrator

# Every remote command is bounded: a hung site must fail the run, not stall it.
SITE_COMMAND_SECONDS = 600


def control_path() -> str:
    """Return the shared SSH control socket pattern (short: sockets have a limit)."""
    return str(Path(tempfile.gettempdir()) / "nl-ssh-%C")


def site_ssh(site: MultipassSite) -> tuple[str, ...]:
    """Return the ssh prefix for commands on a site, over a shared connection."""
    return (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_path()}",
        "-o",
        "ControlPersist=300",
        site.ssh,
    )


def site_jump(site: MultipassSite) -> str:
    """Return the ssh option that reaches a site's VM through the site."""
    # ssh expands %-tokens in ProxyCommand: %h/%p are the VM, while the jump's
    # own ControlPath token must reach the inner ssh unexpanded.
    jump = shlex.join([*site_ssh(site), "-W", "%h:%p"]).replace("%C", "%%C")
    return "ProxyCommand=" + jump


def through_site(
    operations,
    *,
    context,
    host: str,
    site: MultipassSite,
):
    """Point bootstrap operations at a site's VM: its key, jumped via the site."""
    from types import MappingProxyType

    from nanolab.tasks.components.bootstrap import retarget_bootstrap_operation

    common = "-o " + shlex.quote(site_jump(site))
    return tuple(
        replace(
            retargeted,
            env=MappingProxyType({**retargeted.env, "ANSIBLE_SSH_COMMON_ARGS": common}),
        )
        for retargeted in (
            retarget_bootstrap_operation(
                operation,
                context=context,
                host=host,
                private_key=Path(site.vm_ssh_key).expanduser(),
            )
            for operation in operations
        )
    )


def site_argv(site: MultipassSite, argv: tuple[str, ...] | list[str]) -> list[str]:
    """Return the local argv that runs `argv` on the site's host."""
    return [*site_ssh(site), shlex.join(list(argv))]


def remote_path(path: str) -> str:
    """Quote a path for the site's shell, keeping a leading ``~/`` expandable."""
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def site_multipass_argv(site: MultipassSite, *args: str) -> list[str]:
    """Return the local argv that runs the site's Multipass client with `args`."""
    return [*site_ssh(site), " ".join((remote_path(site.multipass), shlex.join(args)))]


class SiteCommandBackend:
    """Run Multipass CLI commands on a site (multipass_vm_sdk CommandBackend).

    `multipass launch --cloud-init <file>` names a file the SDK wrote on the
    operator; it is copied to the site first and the argument rewritten.
    """

    def __init__(self, site: MultipassSite) -> None:
        """Bind the backend to one site."""
        self.site = site

    def _stage(self, local: str) -> str:
        remote = f"/tmp/nl-cloud-init-{Path(local).name}"  # nosec B108 - site-side, per-file name
        subprocess.run(
            [*site_ssh(self.site), f"cat > {shlex.quote(remote)}"],
            input=Path(local).read_bytes(),
            check=True,
            capture_output=True,
            timeout=SITE_COMMAND_SECONDS,
        )
        return remote

    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run one Multipass command on the site and capture its result."""
        remote_args = list(args[1:])
        staged: list[str] = []
        if "--cloud-init" in remote_args:
            index = remote_args.index("--cloud-init") + 1
            remote_args[index] = self._stage(remote_args[index])
            staged.append(remote_args[index])
        try:
            proc = subprocess.run(
                site_multipass_argv(self.site, *remote_args),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=SITE_COMMAND_SECONDS,
                check=False,
            )
        finally:
            for path in staged:
                subprocess.run(
                    site_argv(self.site, ("rm", "-f", path)),
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
        return CommandResult(
            args=args,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


class SiteVmOrchestrator(VmOrchestrator):
    """The local Multipass provider, pointed at a remote site."""

    def __init__(self, repo_root: Path, site: MultipassSite) -> None:
        """Drive the site's daemon and reach its VMs through the site."""
        super().__init__(
            repo_root,
            multipass_client=MultipassClient(
                cmd=site.multipass, backend=SiteCommandBackend(site)
            ),
        )
        self.site = site
        key = Path(site.vm_ssh_key).expanduser()
        self._ssh_public_key = key.with_suffix(".pub").read_text().strip()
        self._private_key_path = key

    def _ssh_options(self) -> list[str]:
        return [*super()._ssh_options(), "-o", site_jump(self.site)]


_READ_FILES = """
import base64, glob, json, sys
out = {}
for pattern in sys.argv[1:]:
    for path in glob.glob(pattern):
        try:
            with open(path, "rb") as handle:
                out[path] = base64.b64encode(handle.read()).decode()
        except OSError:
            pass
print(json.dumps(out))
"""


class SiteHost:
    """A remote Multipass host: commands, capacity and raw /proc and /sys facts."""

    def __init__(self, site: MultipassSite, name: str = "") -> None:
        """Bind to one site; `name` is its key in the environment."""
        self.site = site
        self.name = name or site.ssh

    def run(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        timeout: float = SITE_COMMAND_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        """Run `argv` on the host; a failed command or transport raises."""
        return subprocess.run(
            site_argv(self.site, argv),
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )

    def multipass_argv(self, *args: str) -> list[str]:
        """Return the local argv running the host's Multipass client."""
        return site_multipass_argv(self.site, *args)

    def capacity(self) -> tuple[int, int]:
        """Return the host's CPU count and MemAvailable in MiB."""
        cpus = int(self.run(("nproc", "--all")).stdout.strip())
        meminfo = self.run(("cat", "/proc/meminfo")).stdout
        available = next(
            int(line.split()[1])
            for line in meminfo.splitlines()
            if line.startswith("MemAvailable:")
        )
        return cpus, available // 1024

    def external_vm_cpus(self) -> int:
        """Sum vCPUs of running VMs already on the host, without adopting them."""
        result = subprocess.run(
            self.multipass_argv("info", "--format", "json"),
            check=True,
            capture_output=True,
            text=True,
            timeout=SITE_COMMAND_SECONDS,
        )
        info = json.loads(result.stdout)
        if info.get("errors"):
            raise RuntimeError("external VM inventory is incomplete")
        return sum(
            int(vm["cpu_count"])
            for vm in info["info"].values()
            if vm["state"] == "Running"
        )

    def mirror(self, patterns: list[str], root: Path) -> Path:
        """Copy the host files matching `patterns` under `root`, keeping paths.

        Lets the existing /proc and /sys readers run unchanged against a
        remote host by pointing their root at the mirror.
        """
        files = json.loads(self.run(("python3", "-c", _READ_FILES, *patterns)).stdout)
        for path, content in files.items():
            target = root / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(content))
        return root


_START_FORWARDS = r"""
set -eu
target=$1; listen=$2; shift 2
bind=${listen:+,bind=$listen}
for pair in "$@"; do
  port=${pair%%:*}
  if ss -Hltn "sport = :$port" | grep -q .; then
    echo "published port $port is already in use" >&2; exit 3
  fi
done
for pair in "$@"; do
  port=${pair%%:*}; vm_port=${pair##*:}
  setsid socat "TCP-LISTEN:$port,fork,reuseaddr,nodelay$bind" \
    "TCP:$target:$vm_port,nodelay" </dev/null >/dev/null 2>&1 &
  echo $!
done
"""

_LISTENING = r"""
for port in "$@"; do
  ss -Hltn "sport = :$port" | grep -q . || exit 1
done
"""


class SiteForwarders:
    """User-space `socat` forwarders publishing one VM's ports on its site.

    Each forwarder runs in its own session, so stopping it also stops the
    per-connection children `fork` created.
    """

    def __init__(self, host: SiteHost) -> None:
        """Bind to the site the VM runs on."""
        self.host = host
        self.pids: list[int] = []

    def start(
        self,
        vm_address: str,
        forwards: tuple[tuple[int, int], ...],
        *,
        listen: str = "",
    ) -> None:
        """Publish `(site port, VM port)` pairs and wait until all listen.

        Ports listen on every site address unless `listen` names one.
        """
        if not forwards:
            return
        pairs = [f"{published}:{vm_port}" for published, vm_port in forwards]
        started = self.host.run(
            ("sh", "-c", _START_FORWARDS, "forward", vm_address, listen, *pairs),
            timeout=60,
        )
        self.pids = [int(pid) for pid in started.stdout.split()]
        ports = [str(published) for published, _ in forwards]
        deadline = time.monotonic() + 15
        while True:
            try:
                self.host.run(("sh", "-c", _LISTENING, "listening", *ports), timeout=30)
                return
            except subprocess.CalledProcessError:
                if time.monotonic() > deadline:
                    self.stop()
                    raise RuntimeError(
                        f"forwarders on {self.host.site.ssh} did not listen: {ports}"
                    ) from None
                time.sleep(0.2)

    def stop(self) -> None:
        """Stop every forwarder session this object started; idempotent."""
        if not self.pids:
            return
        groups = [f"-{pid}" for pid in self.pids]
        subprocess.run(
            site_argv(self.host.site, ("kill", "-TERM", "--", *groups)),
            capture_output=True,
            timeout=60,
            check=False,
        )
        alive = subprocess.run(
            site_argv(
                self.host.site,
                ("ps", "-o", "pid=", "-p", ",".join(map(str, self.pids))),
            ),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout.split()
        if alive:
            raise RuntimeError(
                f"forwarders still running on {self.host.site.ssh}: {alive}"
            )
        self.pids = []


class OperatorTunnel:
    """An operator `ssh -L` publishing a node's site ports on an operator address.

    Each port listens on `address` (an address of this operator) and is carried
    to the same port on the site's loopback, where the node's forwarders
    listen. The tunnel has its own SSH connection, so the site's shared command
    connection never carries experiment traffic. A watcher reopens it whenever
    ssh exits; opens, exits and failed reopens go to `journal`.
    """

    def __init__(
        self,
        site: MultipassSite,
        address: str,
        ports: tuple[int, ...],
        *,
        name: str,
        journal: Path | None = None,
        ready_seconds: float = 30,
        retry_seconds: float = 5,
    ) -> None:
        """Store what to publish; nothing runs until `start`."""
        self.site = site
        self.address = address
        self.ports = ports
        self.name = name
        self.journal = journal
        self.ready_seconds = ready_seconds
        self.retry_seconds = retry_seconds
        self.process: subprocess.Popen | None = None
        self._stopping = threading.Event()
        self._watcher: threading.Thread | None = None

    def argv(self) -> list[str]:
        """Return the ssh command holding every forward of this node."""
        forwards = [
            option
            for port in self.ports
            for option in ("-L", f"{self.address}:{port}:127.0.0.1:{port}")
        ]
        return [
            "ssh",
            "-N",
            *("-o", "BatchMode=yes", "-o", "ConnectTimeout=15"),
            *("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"),
            *("-o", "ControlMaster=no", "-o", "ControlPath=none"),
            *("-o", "ExitOnForwardFailure=yes"),
            *forwards,
            self.site.ssh,
        ]

    def _record(self, event: str, **fields) -> None:
        if self.journal is None:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        row = {"atUtc": datetime.now(UTC).isoformat(), "tunnel": self.name}
        with self.journal.open("a") as stream:
            stream.write(json.dumps({**row, "event": event, **fields}) + "\n")

    def _listening(self) -> bool:
        for port in self.ports:
            try:
                socket.create_connection((self.address, port), timeout=1).close()
            except OSError:
                return False
        return True

    def _open(self) -> None:
        self.process = subprocess.Popen(
            self.argv(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + self.ready_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            if self._listening():
                self._record("open", pid=self.process.pid, ports=list(self.ports))
                return
            time.sleep(0.2)
        self._terminate()
        raise RuntimeError(f"tunnel {self.name} did not publish {list(self.ports)}")

    def start(self) -> None:
        """Publish the ports and keep them published until `stop`."""
        self._stopping.clear()
        self._open()
        self._watcher = threading.Thread(
            target=self._watch, name=f"tunnel-{self.name}", daemon=True
        )
        self._watcher.start()

    def _watch(self) -> None:
        while not self._stopping.wait(1):
            process = self.process
            if process is None or process.poll() is None:
                continue
            self._record("exited", returncode=process.returncode)
            while not self._stopping.is_set():
                try:
                    self._open()
                    break
                except (OSError, RuntimeError) as error:
                    self._record("reopen-failed", error=str(error))
                    self._stopping.wait(self.retry_seconds)

    def _terminate(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)

    def stop(self) -> None:
        """Close the tunnel and its watcher; idempotent."""
        self._stopping.set()
        if self._watcher is not None:
            self._watcher.join(timeout=30)
            self._watcher = None
        if self.process is not None:
            self._terminate()
            self._record("closed")
