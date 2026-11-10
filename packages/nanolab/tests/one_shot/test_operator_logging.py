"""The multi-site run's single Loki on the operator and its reverse tunnels."""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanolab.config.environment import OperatorLogging
from nanolab.one_shot import sites
from nanolab.tasks.one_shot import operator_logging
from nanolab.tasks.one_shot.operator_logging import (
    ReverseTunnel,
    log_routes,
)

SITE = sites.MultipassSite(
    ssh="spark", address="149.132.179.35", architecture="arm64", multipass="mp"
)


def logging_config(**overrides):
    data = {"cpus": [6, 7]}
    return OperatorLogging.model_validate({**data, **overrides})


def test_an_exited_tunnel_is_reopened_and_recorded(tmp_path):
    marker = tmp_path / "opened"
    script = (
        "import pathlib, sys, time; p = pathlib.Path(sys.argv[1]); "
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x'); "
        "time.sleep(1.5 if len(p.read_text()) == 1 else 60)"
    )
    tunnel = ReverseTunnel(
        (sys.executable, "-c", script, str(marker)),
        name="spark-35",
        listening=marker.exists,
        directory=tmp_path,
    )
    tunnel.start()
    assert tunnel.process is not None
    tunnel.process.wait(timeout=10)
    tunnel.ensure()
    try:
        deadline = time.monotonic() + 10
        while marker.read_text() != "xx" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.read_text() == "xx"
        assert tunnel.process is not None and tunnel.process.poll() is None
    finally:
        tunnel.stop()
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "tunnels.jsonl").read_text().splitlines()
    ]
    assert events == ["open", "exited", "open", "closed"]


def test_a_tunnel_that_never_listens_fails_and_is_stopped(tmp_path):
    tunnel = ReverseTunnel(
        (sys.executable, "-c", "import time; time.sleep(60)"),
        name="cloud",
        listening=lambda: False,
        directory=tmp_path,
        ready_seconds=0.5,
    )
    with pytest.raises(RuntimeError, match="did not open"):
        tunnel.start()
    assert tunnel.process is None


def test_nodes_on_one_site_share_its_route_and_the_cloud_has_its_own():
    host = sites.SiteHost(SITE, "spark-35")
    edge = SimpleNamespace(
        site_host=host, request=SimpleNamespace(lifecycle="multipass")
    )
    vm = object()
    cloud = SimpleNamespace(
        site_host=None,
        request=SimpleNamespace(lifecycle="azure"),
        config=SimpleNamespace(id="cloud"),
        vm=vm,
    )
    routes = log_routes(
        SimpleNamespace(nodes={"edge-1": edge, "edge-2": edge, "cloud": cloud}),
        logging_config(),
        run_dir=Path("/run"),
    )
    assert routes["edge-1"] is routes["edge-2"]
    # The collector requires the Loki; a route opens without one.
    assert routes["edge-1"].requires == ()
    assert routes["cloud"].requires == (vm,)


def test_site_forwarders_can_listen_on_one_address(tmp_path, monkeypatch):
    calls = []

    def run(self, argv, *, timeout=0):
        calls.append(argv)
        return SimpleNamespace(stdout="4242\n")

    monkeypatch.setattr(sites.SiteHost, "run", run)
    sites.SiteForwarders(sites.SiteHost(SITE)).start(
        "127.0.0.1", ((43100, 43100),), listen="10.83.80.1"
    )
    assert calls[0][3:] == ("forward", "127.0.0.1", "10.83.80.1", "43100:43100")
    assert ",bind=$listen" in calls[0][2]


def test_operator_endpoint_is_the_loopback():
    assert (
        operator_logging.operator_endpoint(logging_config(port=43200))
        == "http://127.0.0.1:43200/loki/api/v1/push"
    )


def test_the_first_multipass_bridge_receives_the_site_logs():
    addresses = (
        "2: enP7s7    inet 149.132.179.35/24 brd 149.132.179.255 scope global\n"
        "14: mpqemubrzone2    inet 10.83.81.1/24 brd 10.83.81.255 scope global\n"
        "13: mpqemubrzone1    inet 10.83.80.1/24 brd 10.83.80.255 scope global\n"
    )
    assert operator_logging.site_bridge(addresses) == "10.83.80.1"
    with pytest.raises(RuntimeError, match="no Multipass bridge"):
        operator_logging.site_bridge(addresses.splitlines()[0])
