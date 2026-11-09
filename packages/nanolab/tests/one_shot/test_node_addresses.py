"""Node addresses: local nodes keep today's URLs, remote nodes use forwarded ports."""

from pathlib import Path

import pytest

import nanolab
from nanolab.one_shot.addresses import (
    advertised_arguments,
    direct,
    management_url,
    published,
    seed_arguments,
)


def test_local_node_keeps_the_historical_control_plane_arguments():
    own, peers = direct("10.83.80.11"), [direct("10.83.80.12"), direct("10.83.80.13")]
    assert advertised_arguments(own) == [
        "--nanofaas.p2p.port=14200",
        "--nanofaas.p2p.external-host=10.83.80.11",
        "--nanofaas.p2p.invocation-uri=http://10.83.80.11:8080",
    ]
    assert seed_arguments(peers) == [
        "--nanofaas.p2p.seeds[0]=10.83.80.12:14200",
        "--nanofaas.p2p.seeds[1]=10.83.80.13:14200",
    ]


def test_local_node_keeps_the_historical_urls():
    node = direct("10.83.80.11")
    assert node.api == "http://10.83.80.11:8080"
    assert node.management == "http://10.83.80.11:9090"
    assert node.probe == "http://10.83.80.11:17111"
    assert node.forwards() == ()


def test_published_node_advertises_the_site_address_and_its_own_ports():
    own = published("149.132.179.35", 3)
    assert advertised_arguments(own) == [
        "--nanofaas.p2p.port=14203",
        "--nanofaas.p2p.external-host=149.132.179.35",
        "--nanofaas.p2p.invocation-uri=http://149.132.179.35:18003",
    ]
    assert own.management == "http://149.132.179.35:19013"
    assert own.probe == "http://149.132.179.35:17203"
    assert own.forwards() == (
        (18003, 8080),
        (19013, 9090),
        (14203, 14203),
        (17203, 17111),
        (19203, 9100),
        (19303, 8089),
    )
    assert own.exporters() == {
        "node-exporter": "149.132.179.35:19203",
        "cadvisor": "149.132.179.35:19303",
    }


def test_published_ports_never_collide_between_nodes_of_a_site():
    ports = [
        port for n in range(1, 12) for port, _ in published("10.0.0.1", n).forwards()
    ]
    assert len(ports) == len(set(ports))


@pytest.mark.parametrize("ordinal", [0, 100])
def test_published_ordinal_is_bounded(ordinal):
    with pytest.raises(ValueError, match="ordinal"):
        published("10.0.0.1", ordinal)


@pytest.mark.parametrize(
    ("api", "management"),
    [
        ("http://10.83.80.11:8080", "http://10.83.80.11:9090"),
        ("http://149.132.179.35:18003", "http://149.132.179.35:19013"),
        ("http://20.1.2.3:8080/", "http://20.1.2.3:9090"),
    ],
)
def test_management_url_follows_the_api_port(api, management):
    assert management_url(api) == management


def test_management_url_keeps_a_portless_url_like_the_historical_rewrite():
    assert management_url("http://edge0") == "http://edge0"


def test_management_url_requires_a_host():
    with pytest.raises(ValueError, match="explicit host"):
        management_url("/v1/functions")


def test_one_shot_code_no_longer_rewrites_api_urls_by_string():
    root = Path(nanolab.__file__).parent
    sources = [
        *root.joinpath("one_shot").rglob("*.py"),
        *root.joinpath("tasks", "one_shot").rglob("*.py"),
        *root.joinpath("plans").glob("one_shot*.py"),
    ]
    offenders = [
        str(path)
        for path in sources
        # addresses.py documents the rewrite it replaces.
        if path.name != "addresses.py"
        and ('replace(":8080"' in path.read_text() or ":9090/" in path.read_text())
    ]
    assert offenders == []
