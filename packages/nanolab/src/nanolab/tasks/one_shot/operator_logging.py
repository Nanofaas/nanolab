"""One Loki on the operator for a multi-site run, reached through reverse tunnels.

The single-host workflows run Loki with the operator's Docker on the Multipass
bridge. A multi-site operator need not have Docker and is reachable from the
sites only on a public address, so Loki runs from a pinned release binary on
the operator's loopback. Each site and the cloud reach it through a reverse
SSH tunnel ending on their own loopback: a site publishes it on its Multipass
bridge with socat, the cloud's collector (host network) pushes to it directly.
Fluent Bit buffers on disk and retries without limit, so a tunnel that drops is
restarted without losing records; delivery is still verified per collector.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from sonata_engine import Resource, TaskInputs
from sonata_tasks.compensation import compensated_resource

from nanolab.config.environment import OperatorLogging
from nanolab.config.one_shot import OneShotLogs, operational_limit
from nanolab.one_shot.sites import SiteForwarders, SiteHost
from nanolab.tasks.one_shot.logging import verify_loki_ingestion

PUSH_PATH = "/loki/api/v1/push"
# Multipass names its bridges mpqemubr* (one per zone on recent releases);
# the host forwards between them, so a VM in any zone reaches the first one.
SITE_BRIDGES = "mpqemubr"


def operator_endpoint(logging: OperatorLogging) -> str:
    """Return the push URL of the operator's Loki, as the operator reaches it."""
    return f"http://127.0.0.1:{logging.port}{PUSH_PATH}"


def loki_config(template: str) -> str:
    """Point the shipped Loki configuration's storage at a run directory.

    The container image keeps its data under `/loki`; the native process keeps
    it under its working directory and advertises its loopback in the ring.
    """
    config, count = re.subn(r"(?m)(:\s+)/loki\b", r"\1.", template)
    if count == 0 or "\ncommon:\n" not in config:
        raise ValueError("unexpected Loki configuration template")
    return config.replace("\ncommon:\n", "\ncommon:\n  instance_addr: 127.0.0.1\n", 1)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


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


def operator_loki_resource(
    logs: OneShotLogs,
    logging: OperatorLogging,
    *,
    root: Path,
    run_dir: Path,
    operator: Resource | None = None,
    settings=None,
    supervision=None,
) -> Resource[str]:
    """Own the run's Loki as a pinned process; its data is the raw archive."""
    if not logs.backend_owned:
        raise ValueError("a multi-site run owns its logging backend")
    endpoint = operator_endpoint(logging)
    project = "nanolab-logs-" + uuid4().hex[:12]
    directory = run_dir / "logging" / "backend"
    data = directory / "backend-data"
    state: dict[str, subprocess.Popen] = {}

    def acquire(_inputs):
        binary = Path(logging.loki_binary).expanduser()
        if _sha256(binary) != logging.loki_sha256:
            raise ValueError(f"Loki binary {binary} does not match its sha256")
        data.mkdir(parents=True, exist_ok=False)
        config = directory / "loki.yaml"
        config.write_text(
            loki_config((root / "deploy/observability/loki.yaml").read_text())
        )
        argv = [
            "taskset",
            "-c",
            ",".join(map(str, logging.cpus)),
            str(binary),
            f"-config.file={config}",
            "-server.http-listen-address=127.0.0.1",
            f"-server.http-listen-port={logging.port}",
            "-server.grpc-listen-address=127.0.0.1",
            f"-server.grpc-listen-port={logging.port + 1}",
            # As for the container: retain the WAL and check real IO instead of
            # a percent-full cutoff on a disk shared with other data.
            "-ingester.wal-disk-full-threshold=0",
            "-ingester.wal-enabled=true",
        ]
        with (directory / "backend.log").open("ab") as log:
            state["process"] = process = subprocess.Popen(
                argv,
                cwd=data,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (directory / "logging-host-placement.json").write_text(
            json.dumps(
                {
                    "host": "operator",
                    "pid": process.pid,
                    "cpus": sorted(os.sched_getaffinity(process.pid)),
                    "binary": str(binary),
                    "binarySha256": logging.loki_sha256,
                    "argv": argv,
                },
                indent=2,
            )
            + "\n"
        )
        deadline = time.monotonic() + operational_limit(
            settings, "service_start_seconds", 90
        )
        ready = endpoint.removesuffix(PUSH_PATH) + "/ready"
        with httpx.Client(trust_env=False) as http:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Loki exited with status {process.returncode}")
                try:
                    http.get(ready, timeout=2).raise_for_status()
                except httpx.HTTPError:
                    time.sleep(0.5)
                    continue
                verify_loki_ingestion(endpoint, project, directory, settings)
                return endpoint
        raise RuntimeError("operator Loki readiness failed")

    def release(_inputs, _value=None):
        process = state.pop("process", None)
        if process is None:
            return
        graceful = _stop(process, operational_limit(settings, "compose_seconds", 90))
        hashes = {
            str(path.relative_to(data)): _sha256(path)
            for path in sorted(data.rglob("*"))
            if path.is_file()
        }
        (directory / "backend-data-sha256.json").write_text(
            json.dumps(hashes, indent=2) + "\n"
        )
        if not graceful:
            raise RuntimeError("operator Loki ignored SIGTERM and was killed")

    resource = compensated_resource(
        title="Own operator Loki logging backend",
        acquire=acquire,
        release=release,
        compensate=release,
        requires=(operator,) if operator is not None else (),
    )
    if supervision is None:
        return resource

    def probe(_value):
        process = state.get("process")
        if process is None or process.poll() is not None:
            raise RuntimeError("operator Loki stopped")
        verify_loki_ingestion(endpoint, project, directory, settings)

    return supervision.service_resource(
        resource,
        "logging-backend",
        probe,
        join_seconds=operational_limit(settings, "worker_join_seconds", 40),
    )


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
    loki: Resource[str],
    *,
    run_dir: Path,
    settings=None,
    supervision=None,
) -> dict[str, Resource[str]]:
    """Return, per node, the route its collector pushes through to the Loki."""
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
                sites[name] = _supervised(
                    compensated_resource(
                        title=f"Route {name} logs to the operator",
                        acquire=acquire,
                        release=release,
                        compensate=release,
                        requires=(loki,),
                    ),
                    f"log-route:{name}",
                    tunnel.ensure,
                    supervision,
                    settings,
                )
            routes[key] = sites[name]
        elif node.request.lifecycle == "azure":
            routes[key] = _cloud_route(
                node, logging, loki, directory, settings, supervision
            )
        else:
            raise ValueError("multi-site logging needs every node on a site or Azure")
    return routes


def _cloud_route(node, logging, loki, directory, settings, supervision):
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

    return _supervised(
        compensated_resource(
            title=f"Route {node.config.id} logs to the operator",
            acquire=acquire,
            release=release,
            compensate=release,
            requires=(loki, node.vm),
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
