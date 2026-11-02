"""Where the operator and the peers reach each node's services.

A node on the local Multipass daemon or on Azure is reached on its own address
and the fixed in-VM ports. A node on a remote Multipass site sits behind that
host's NAT, so its services are published on the site's address through
per-node forwarded ports. Every caller builds URLs from `NodeAddresses` instead
of from a VM address, so the two cases differ only here.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

API_PORT = 8080
MANAGEMENT_PORT = 9090
GOSSIP_PORT = 14200
PROBE_PORT = 17111

# The management port keeps the in-VM distance from the API port on published
# nodes too, so a management URL is derived from any API URL the same way.
MANAGEMENT_OFFSET = MANAGEMENT_PORT - API_PORT

PUBLISHED_API_BASE = 18000
PUBLISHED_GOSSIP_BASE = GOSSIP_PORT
PUBLISHED_PROBE_BASE = 17200
MAX_PUBLISHED_NODES = 99


@dataclass(frozen=True)
class NodeAddresses:
    """One node's reachable host and ports, as seen by operator and peers."""

    host: str
    api_port: int = API_PORT
    gossip_port: int = GOSSIP_PORT
    probe_port: int = PROBE_PORT
    published: bool = False

    @property
    def api(self) -> str:
        """Return the control-plane URL used for control, load and invocations."""
        return f"http://{self.host}:{self.api_port}"

    @property
    def management(self) -> str:
        """Return the management URL (readiness, Prometheus)."""
        return management_url(self.api)

    @property
    def probe(self) -> str:
        """Return the clock/inventory/bandwidth probe URL."""
        return f"http://{self.host}:{self.probe_port}"

    @property
    def gossip(self) -> str:
        """Return the p2p seed address."""
        return f"{self.host}:{self.gossip_port}"

    def forwards(self) -> tuple[tuple[int, int], ...]:
        """Return (published port, VM port) pairs a published node needs."""
        if not self.published:
            return ()
        return (
            (self.api_port, API_PORT),
            (self.api_port + MANAGEMENT_OFFSET, MANAGEMENT_PORT),
            # Gossip binds the published port inside the VM: scalecube
            # advertises its bind port, so the two must be equal.
            (self.gossip_port, self.gossip_port),
            (self.probe_port, PROBE_PORT),
        )


def direct(host: str) -> NodeAddresses:
    """Address a node on its own address and the fixed in-VM ports."""
    return NodeAddresses(host)


def published(site_address: str, ordinal: int) -> NodeAddresses:
    """Address the `ordinal`-th node (1-based) on its site's forwarded ports."""
    if not 1 <= ordinal <= MAX_PUBLISHED_NODES:
        raise ValueError("published node ordinal out of range")
    return NodeAddresses(
        site_address,
        api_port=PUBLISHED_API_BASE + ordinal,
        gossip_port=PUBLISHED_GOSSIP_BASE + ordinal,
        probe_port=PUBLISHED_PROBE_BASE + ordinal,
        published=True,
    )


def management_url(api_url: str) -> str:
    """Return the management URL that belongs to a control-plane API URL.

    A URL without an explicit port is returned unchanged, as the historical
    `replace(":8080", ":9090")` did: fakes and proxies that serve both on one
    address keep working.
    """
    parts = urlsplit(api_url)
    if parts.hostname is None:
        raise ValueError(f"API URL needs an explicit host: {api_url}")
    if parts.port is None:
        return api_url.rstrip("/")
    netloc = f"{parts.hostname}:{parts.port + MANAGEMENT_OFFSET}"
    return urlunsplit((parts.scheme, netloc, "", "", ""))


def advertised_arguments(own: NodeAddresses) -> list[str]:
    """Return the control-plane arguments that bind and advertise p2p."""
    return [
        f"--nanofaas.p2p.port={own.gossip_port}",
        f"--nanofaas.p2p.external-host={own.host}",
        f"--nanofaas.p2p.invocation-uri={own.api}",
    ]


def seed_arguments(peers: list[NodeAddresses]) -> list[str]:
    """Return the control-plane arguments that seed p2p discovery."""
    return [
        f"--nanofaas.p2p.seeds[{index}]={peer.gossip}"
        for index, peer in enumerate(peers)
    ]
