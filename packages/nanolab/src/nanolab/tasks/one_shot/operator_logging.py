"""One Loki on the operator for a multi-site run, reached through reverse tunnels.

The run's Loki is the workbench platform's container, published on the
operator's loopback (`OperatorLogging.port`): the operator is reachable from
the sites only on a public address. Each site and the cloud reach it through
a reverse SSH tunnel ending on their own loopback: a site publishes it on its
Multipass bridge with socat, the cloud's collector (host network) pushes to it directly.
Fluent Bit buffers on disk and retries without limit, so a tunnel that drops is
restarted without losing records; delivery is still verified per collector.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sonata_engine import Resource, TaskInputs
from sonata_tasks.compensation import compensated_resource

from nanolab.config.environment import OperatorLogging
from nanolab.config.one_shot import operational_limit
from nanolab.one_shot.sites import SiteForwarders, SiteHost
from nanolab.tasks.resource_tasks import call, release_call

PUSH_PATH = "/loki/api/v1/push"
# Multipass names its bridges mpqemubr* (one per zone on recent releases);
# the host forwards between them, so a VM in any zone reaches the first one.
SITE_BRIDGES = "mpqemubr"


def operator_endpoint(logging: OperatorLogging) -> str:
    """Return the push URL of the operator's Loki, as the operator reaches it."""
    return f"http://127.0.0.1:{logging.port}{PUSH_PATH}"


def _stop(process: subprocess.Popen, seconds: float) -> bool:
    """Terminate a process group; return whether it exited without a kill."""
    if process.poll() is not None:
        return True
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=30)
        return False


class ReverseTunnel:
    """An `ssh -R` process, restarted whenever it exits; restarts are recorded."""

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        name: str,
        listening: Callable[[], bool],
        directory: Path,
        ready_seconds: float = 30,
    ) -> None:
        """Store the tunnel command and how to tell that its far end listens."""
        self.argv = argv
        self.name = name
        self.listening = listening
        self.directory = directory
        self.ready_seconds = ready_seconds
        self.process: subprocess.Popen | None = None

    def _record(self, event: str, **fields) -> None:
        row = {"atUtc": datetime.now(UTC).isoformat(), "tunnel": self.name}
        with (self.directory / "tunnels.jsonl").open("a") as stream:
            stream.write(json.dumps({**row, "event": event, **fields}) + "\n")

    def start(self) -> None:
        """Open the tunnel and wait until its far end accepts connections."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / f"tunnel-{self.name}.log").open("ab") as log:
            self.process = subprocess.Popen(
                self.argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + self.ready_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            if self.listening():
                self._record("open", pid=self.process.pid)
                return
            time.sleep(0.5)
        self.stop()
        raise RuntimeError(f"log tunnel {self.name} did not open")

    def ensure(self) -> None:
        """Reopen a tunnel that exited; fail only when it cannot be reopened."""
        if self.process is not None and self.process.poll() is None:
            return
        code = None if self.process is None else self.process.returncode
        self._record("exited", returncode=code)
        self.start()

    def stop(self) -> None:
        """Close the tunnel; idempotent."""
        if self.process is not None:
            _stop(self.process, 10)
            self._record("closed", returncode=self.process.returncode)
            self.process = None


def _reverse(port: int) -> tuple[str, ...]:
    return (
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-R",
        f"127.0.0.1:{port}:127.0.0.1:{port}",
    )


_LISTENS = 'ss -Hltn "sport = :$0 and src = 127.0.0.1" | grep -q .'


def _succeeds(run: Callable[[], object]) -> bool:
    try:
        run()
    except subprocess.CalledProcessError:
        return False
    return True


def site_bridge(addresses: str) -> str:
    """Return the first Multipass bridge address in `ip -4 -o addr` output."""
    bridges = sorted(
        (fields[1], fields[3].split("/")[0])
        for fields in (line.split() for line in addresses.splitlines())
        if len(fields) > 3 and fields[1].startswith(SITE_BRIDGES)
    )
    if not bridges:
        raise RuntimeError("the site has no Multipass bridge")
    return bridges[0][1]


def _site_route(
    host: SiteHost, logging: OperatorLogging, directory: Path
) -> tuple[Callable[[TaskInputs], str], Callable[..., None], ReverseTunnel]:
    port = logging.port
    site = host.site
    tunnel = ReverseTunnel(
        (
            "ssh",
            *("-o", "BatchMode=yes", "-o", "ConnectTimeout=15"),
            *("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"),
            *("-o", "ControlMaster=no", "-o", "ControlPath=none"),
            *_reverse(port),
            site.ssh,
        ),
        name=host.name,
        listening=lambda: _succeeds(
            lambda: host.run(("sh", "-c", _LISTENS, str(port)), timeout=30)
        ),
        directory=directory,
    )
    forwarders = SiteForwarders(host)

    def acquire(_inputs) -> str:
        bridge = site_bridge(host.run(("ip", "-4", "-o", "addr", "show")).stdout)
        # The bridge listener first: the tunnel then takes the loopback port.
        forwarders.start("127.0.0.1", ((port, port),), listen=bridge)
        try:
            tunnel.start()
        except BaseException:
            forwarders.stop()
            raise
        return f"http://{bridge}:{port}{PUSH_PATH}"

    def release(_inputs, _value=None) -> None:
        try:
            tunnel.stop()
        finally:
            forwarders.stop()

    return acquire, release, tunnel


def log_routes(
    resources,
    logging: OperatorLogging,
    *,
    run_dir: Path,
    settings=None,
    supervision=None,
) -> dict[str, Resource[str]]:
    """Return, per node, the route its collector pushes through to the Loki.

    A route carries the operator's loopback `logging.port`, where the run's
    Loki publishes; it needs no running Loki to open (Fluent Bit retries), so
    the collector, not the route, requires the Loki.
    """
    directory = run_dir / "logging"
    routes: dict[str, Resource[str]] = {}
    sites: dict[str, Resource[str]] = {}
    for key, node in resources.nodes.items():
        if node.site_host is not None:
            name = node.site_host.name
            if name not in sites:
                acquire, release, tunnel = _site_route(
                    node.site_host, logging, directory
                )
                title = f"Route {name} logs to the operator"
                sites[name] = _supervised(
                    compensated_resource(
                        title=title,
                        acquire=call(title, acquire),
                        release=release_call(title, release),
                        compensate=call(f"Compensate {title}", release),
                    ),
                    f"log-route:{name}",
                    tunnel.ensure,
                    supervision,
                    settings,
                )
            routes[key] = sites[name]
        elif node.request.lifecycle == "azure":
            routes[key] = _cloud_route(node, logging, directory, settings, supervision)
        else:
            raise ValueError("multi-site logging needs every node on a site or Azure")
    return routes


def _cloud_route(node, logging, directory, settings, supervision):
    port = logging.port
    holder: dict[str, ReverseTunnel] = {}

    def acquire(inputs: TaskInputs) -> str:
        vm = inputs.resource(node.vm)
        dedicated = node.azure_ssh(vm, shared=False)
        holder["tunnel"] = tunnel = ReverseTunnel(
            (*dedicated[:-1], *_reverse(port), dedicated[-1]),
            name=node.config.id,
            listening=lambda: _succeeds(
                lambda: subprocess.run(
                    node.vm_argv(vm, "sh", "-c", _LISTENS, str(port)),
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
            ),
            directory=directory,
        )
        tunnel.start()
        # The collector shares the VM's network: the tunnel's loopback end.
        return f"http://127.0.0.1:{port}{PUSH_PATH}"

    def release(_inputs, _value=None) -> None:
        tunnel = holder.pop("tunnel", None)
        if tunnel is not None:
            tunnel.stop()

    def ensure() -> None:
        tunnel = holder.get("tunnel")
        if tunnel is None:
            raise RuntimeError("cloud log tunnel is not open")
        tunnel.ensure()

    title = f"Route {node.config.id} logs to the operator"
    return _supervised(
        compensated_resource(
            title=title,
            acquire=call(title, acquire),
            release=release_call(title, release),
            compensate=call(f"Compensate {title}", release),
            requires=(node.vm,),
        ),
        f"log-route:{node.config.id}",
        ensure,
        supervision,
        settings,
    )


def _supervised(resource, name, ensure, supervision, settings):
    if supervision is None:
        return resource
    return supervision.service_resource(
        resource,
        name,
        lambda _value: ensure(),
        join_seconds=operational_limit(settings, "worker_join_seconds", 40),
    )
