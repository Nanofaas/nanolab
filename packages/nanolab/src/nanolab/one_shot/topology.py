"""Operator-owned sparse peer graph, separate from discovery bootstrap seeds."""

from __future__ import annotations

from nanolab.config.one_shot import OneShotConfig


def validate_neighbor_graph(settings: OneShotConfig) -> dict[str, tuple[str, ...]]:
    """Require a connected undirected edge-only graph with exact identities."""
    edges = {node.id for node in settings.nodes if node.kind == "edge"}
    graph = settings.neighbors
    if graph is None:
        return {node: tuple(sorted(edges - {node})) for node in sorted(edges)}
    if set(graph) != edges:
        raise ValueError("neighbor graph must name every edge and no cloud")
    for node, peers in graph.items():
        if len(peers) != len(set(peers)) or node in peers or set(peers) - edges:
            raise ValueError("invalid neighbor identity, duplicate or self-loop")
        if any(node not in graph[peer] for peer in peers):
            raise ValueError("neighbor graph must be symmetric")
    seen, pending = set(), [min(edges)]
    while pending:
        node = pending.pop()
        if node not in seen:
            seen.add(node)
            pending.extend(graph[node])
    if seen != edges:
        raise ValueError("neighbor graph must be connected")
    return {node: tuple(sorted(peers)) for node, peers in sorted(graph.items())}


def operator_peer_modes(
    node_id: str, graph: dict[str, tuple[str, ...]]
) -> dict[str, str]:
    """Persist exclusion of non-neighbors even if gossip discovers them later."""
    return {
        peer: "FORCE_ACTIVE" if peer in graph[node_id] else "EXCLUDED"
        for peer in graph
        if peer != node_id
    }


def verify_neighbor_sets(
    observed: dict[str, set[str]], expected: dict[str, tuple[str, ...]]
) -> None:
    """Equal counts cannot hide a wrong or rediscovered operative peer."""
    if set(observed) != set(expected) or any(
        observed[node] != set(peers) for node, peers in expected.items()
    ):
        raise ValueError("operative neighbor identities differ from frozen graph")


def observe_neighbors(
    http, endpoints: dict[str, str], expected: dict[str, tuple[str, ...]]
) -> dict:
    """Check both discovery-active neighbors and one-shot endpoints, retaining RTT."""
    from nanolab.one_shot.client import NanoFaasOneShotClient

    snapshots, operative, discovery = {}, {}, {}
    for node, url in endpoints.items():
        status = NanoFaasOneShotClient(url, http=http).status()
        response = http.get(url + "/v1/admin/p2p/peers", timeout=3)
        response.raise_for_status()
        peers = response.json()
        active = [peer["id"] for peer in peers if peer["active"]]
        identities = [peer.peer_id for peer in status.peer_endpoints]
        if len(active) != len(set(active)) or len(identities) != len(set(identities)):
            raise ValueError("duplicate operative neighbor identity")
        operative[node], discovery[node] = set(identities), set(active)
        if any(
            peer["mode"] != "EXCLUDED"
            for peer in peers
            if peer["id"] in expected
            and peer["id"] not in expected[node]
            and peer["id"] != node
        ):
            raise ValueError("non-neighbor mode drifted from EXCLUDED")
        snapshots[node] = {
            "peers": peers,
            "endpoints": [
                peer.model_dump(by_alias=True) for peer in status.peer_endpoints
            ],
        }
    verify_neighbor_sets(operative, expected)
    verify_neighbor_sets(discovery, expected)
    return snapshots
