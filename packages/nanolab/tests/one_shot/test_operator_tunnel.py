"""A site behind an SSH-only NAT is published on the operator through ssh -L."""

import json
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nanolab.config.environment import MultipassSite
from nanolab.config.one_shot import OneShotNode
from nanolab.one_shot import infrastructure
from nanolab.one_shot.sites import OperatorTunnel, SiteHost
from nanolab.tasks.vm.models import VmInfo, VmRequest

SITE = MultipassSite(
    ssh="spark-175",
    address="149.132.176.50",
    architecture="arm64",
    tunnel=True,
)

# Stands in for ssh -L: listens on every given port of 127.0.0.1 until killed.
LISTENER = """import socket, sys, time
sockets = []
for port in sys.argv[1:]:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", int(port)))
    s.listen()
    sockets.append(s)
while True:
    time.sleep(1)
"""


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    for item in sockets:
        item.bind(("127.0.0.1", 0))
    ports = tuple(item.getsockname()[1] for item in sockets)
    for item in sockets:
        item.close()
    return ports


def journal_events(path: Path):
    return [json.loads(line)["event"] for line in path.read_text().splitlines()]


def test_the_tunnel_forwards_each_port_to_the_site_loopback():
    argv = OperatorTunnel(SITE, "149.132.176.50", (18006, 14206), name="edge-6").argv()
    assert argv[:2] == ["ssh", "-N"]
    assert argv[-1] == "spark-175"
    assert "ExitOnForwardFailure=yes" in argv and "ControlPath=none" in argv
    forwards = [argv[i + 1] for i, value in enumerate(argv) if value == "-L"]
    assert forwards == [
        "149.132.176.50:18006:127.0.0.1:18006",
        "149.132.176.50:14206:127.0.0.1:14206",
    ]


def test_an_exited_tunnel_is_reopened_and_every_change_journaled(tmp_path, monkeypatch):
    script = tmp_path / "listener.py"
    script.write_text(LISTENER)
    ports = free_ports(2)
    journal = tmp_path / "tunnels.jsonl"
    tunnel = OperatorTunnel(SITE, "127.0.0.1", ports, name="edge-6", journal=journal)
    monkeypatch.setattr(
        tunnel, "argv", lambda: [sys.executable, str(script), *map(str, ports)]
    )
    tunnel.start()
    try:
        first = tunnel.process
        assert first is not None
        socket.create_connection(("127.0.0.1", ports[1]), timeout=1).close()
        first.kill()
        deadline = time.monotonic() + 20
        while journal_events(journal)[-1] != "open" or tunnel.process is first:
            assert time.monotonic() < deadline, journal_events(journal)
            time.sleep(0.2)
        socket.create_connection(("127.0.0.1", ports[0]), timeout=1).close()
    finally:
        tunnel.stop()
    assert journal_events(journal) == ["open", "exited", "open", "closed"]
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", ports[0]), timeout=1).close()


def test_a_tunnel_that_cannot_publish_fails_and_leaves_nothing_running(
    tmp_path, monkeypatch
):
    tunnel = OperatorTunnel(
        SITE, "127.0.0.1", free_ports(1), name="edge-6", ready_seconds=1
    )
    monkeypatch.setattr(tunnel, "argv", lambda: [sys.executable, "-c", "pass"])
    with pytest.raises(RuntimeError, match="did not publish"):
        tunnel.start()
    assert tunnel.process is None


@pytest.mark.parametrize("tunnelled", [True, False])
def test_a_site_node_is_published_and_unpublished_in_order(
    tmp_path, monkeypatch, tunnelled
):
    events: list = []

    class Forwarders:
        def __init__(self, _host):
            pass

        def start(self, host, forwards):
            events.append(("forward", host, forwards))

        def stop(self):
            events.append("unforward")

    class Tunnel:
        def __init__(self, site, address, ports, *, name, journal):
            events.append(("tunnel", address, ports, name, journal))

        def start(self):
            events.append("open")

        def stop(self):
            events.append("close")

    vm = VmInfo(name="nl-os-r-edge-6", host="10.0.0.5", user="ubuntu", home="/h")
    base = SimpleNamespace(
        title="Acquire edge-6 VM",
        requires=(),
        revive=None,
        acquire=lambda _inputs: vm,
        release=lambda _inputs, _value: events.append("release"),
    )
    monkeypatch.setattr(infrastructure, "SiteForwarders", Forwarders)
    monkeypatch.setattr(infrastructure, "OperatorTunnel", Tunnel)
    monkeypatch.setattr(infrastructure, "provisioned_vm", lambda **_kwargs: base)
    site = SITE.model_copy(update={"tunnel": tunnelled})
    node = infrastructure._owned_node(
        OneShotNode(id="edge-6", kind="edge"),
        VmRequest(lifecycle="multipass", name="nl-os-r-edge-6"),
        SimpleNamespace(vm_exists=lambda _request: False),
        config=cast(Any, None),
        generator=cast(Any, None),
        target_cpus=None,
        pin=lambda *_args: None,
        stop_argv=None,
        site_host=SiteHost(site, "spark-175"),
        ordinal=6,
        tunnel_journal=tmp_path / "tunnels.jsonl",
    )
    acquired = node.vm.acquire(cast(Any, None))
    node.vm.release(cast(Any, None), acquired)
    forwards = ((18006, 8080), (19016, 9090), (14206, 14206), (17206, 17111))
    published = [("forward", "10.0.0.5", forwards)]
    if tunnelled:
        assert events == [
            (
                "tunnel",
                "149.132.176.50",
                (18006, 19016, 14206, 17206),
                "edge-6",
                tmp_path / "tunnels.jsonl",
            ),
            *published,
            "open",
            "close",
            "unforward",
            "release",
        ]
    else:
        assert events == [*published, "unforward", "release"]
