from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from sonata_engine import TaskInputs

from nanolab.tasks.one_shot.resources import node_probe_resource


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
