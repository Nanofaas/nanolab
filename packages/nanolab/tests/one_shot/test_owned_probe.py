import runpy
import socket
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from threading import Thread
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from sonata_engine import TaskInputs

from nanolab.tasks.one_shot.resources import node_probe_resource


def test_actual_probe_disables_nagle_on_persistent_clock_connection(monkeypatch):
    asset = files("nanolab").joinpath("assets/one-shot/probe.py")
    with monkeypatch.context() as staged:
        staged.setattr(
            "http.server.ThreadingHTTPServer",
            lambda *_: SimpleNamespace(serve_forever=lambda: None),
        )
        handler = cast(
            type[BaseHTTPRequestHandler], runpy.run_path(str(asset))["Handler"]
        )
    socket_options = []

    class ObservedHandler(handler):
        def setup(self):
            super().setup()
            socket_options.append(
                self.connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), ObservedHandler)
    worker = Thread(target=server.serve_forever)
    worker.start()
    try:
        with httpx.Client(trust_env=False) as http:
            for _ in range(3):
                response = http.get(
                    f"http://127.0.0.1:{server.server_port}/clock", timeout=2
                )
                response.raise_for_status()
                assert isinstance(response.json()["timestamp"], float)
        assert socket_options == [1]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


@pytest.mark.parametrize("transfer_fails", [False, True])
def test_probe_is_staged_readied_and_released_or_reports_failed_transfer(
    monkeypatch, transfer_fails
):
    commands = []

    def command(argv):
        commands.append(argv)
        return SimpleNamespace(stdout="1234")

    node = cast(
        Any,
        SimpleNamespace(
            config=SimpleNamespace(id="edge-0"),
            request=object(),
            vm="vm",
            provider=SimpleNamespace(
                transfer_to=lambda *_args, **_kwargs: SimpleNamespace(
                    return_code=1 if transfer_fails else 0
                )
            ),
            command=command,
        ),
    )
    inputs = cast(
        TaskInputs,
        SimpleNamespace(
            resource=lambda _: SimpleNamespace(home="/home/ubuntu", host="10.0.0.1")
        ),
    )
    owned = node_probe_resource(node)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as http:
        monkeypatch.setattr("httpx.Client", lambda **_: nullcontext(http))
        if transfer_fails:
            with pytest.raises(RuntimeError, match="stage"):
                owned.acquire(inputs)
        else:
            url = owned.acquire(inputs)
            assert url == "http://10.0.0.1:17111"
            owned.release(inputs, url)
            assert commands[-1] == ("kill", "1234")
