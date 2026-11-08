"""The multi-site run's single Loki on the operator and its reverse tunnels."""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanolab.config.environment import OperatorLogging
from nanolab.config.one_shot import OneShotLogs
from nanolab.one_shot import sites
from nanolab.tasks.one_shot import operator_logging
from nanolab.tasks.one_shot.operator_logging import (
    ReverseTunnel,
    log_routes,
    loki_config,
    operator_loki_resource,
)
from nanolab.tasks.resource_tasks import acquire_now

TEMPLATE = """\
auth_enabled: false
server:
  http_listen_port: 3100
common:
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
compactor:
  working_directory: /loki/compactor
"""

SITE = sites.MultipassSite(
    ssh="spark", address="149.132.179.35", architecture="arm64", multipass="mp"
)


def logging_config(**overrides):
    data = {"lokiBinary": "/opt/loki", "lokiSha256": "0" * 64, "cpus": [6, 7]}
    return OperatorLogging.model_validate({**data, **overrides})


def test_loki_storage_moves_under_the_working_directory():
    config = loki_config(TEMPLATE)
    assert "/loki" not in config
    assert "path_prefix: ." in config
    assert "chunks_directory: ./chunks" in config
    assert "common:\n  instance_addr: 127.0.0.1\n  path_prefix: ." in config


def test_an_unexpected_loki_template_is_refused():
    with pytest.raises(ValueError, match="unexpected Loki configuration"):
        loki_config("server:\n  http_listen_port: 3100\n")


def test_the_shipped_loki_template_is_rewritten(nanofaas_root):
    path = nanofaas_root / "deploy/observability/loki.yaml"
    assert "/loki" not in loki_config(path.read_text())


def test_a_loki_binary_with_another_digest_never_starts(tmp_path):
    binary = tmp_path / "loki"
    binary.write_bytes(b"not loki")
    root = tmp_path / "nanofaas"
    (root / "deploy/observability").mkdir(parents=True)
    (root / "deploy/observability/loki.yaml").write_text(TEMPLATE)
    resource = operator_loki_resource(
        OneShotLogs.model_validate(
            {
                "enabled": True,
                "endpoint": "http://10.83.80.1:43100/loki/api/v1/push",
                "backendOwned": True,
                "backendBind": "10.83.80.1",
            }
        ),
        logging_config(lokiBinary=str(binary)),
        root=root,
        run_dir=tmp_path / "run",
    )
    with pytest.raises(ValueError, match="does not match its sha256"):
        acquire_now(resource, None)  # type: ignore[arg-type]
    assert not (tmp_path / "run/logging/backend/backend-data").exists()


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
    loki = object()
    routes = log_routes(
        SimpleNamespace(nodes={"edge-1": edge, "edge-2": edge, "cloud": cloud}),
        logging_config(),
        loki,  # type: ignore[arg-type]
        run_dir=Path("/run"),
    )
    assert routes["edge-1"] is routes["edge-2"]
    assert routes["edge-1"].requires == (loki,)
    assert routes["cloud"].requires == (loki, vm)


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
