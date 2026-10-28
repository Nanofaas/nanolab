"""tinyFaaS, Serverledge and Fn: started in the budget, configured from the protocol."""

import json
import os
import subprocess
from pathlib import PurePosixPath

import yaml

from nanolab.config.experiment_02 import Budget
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.experiment_02.baselines import Fn, Serverledge, TinyFaas
from nanolab.tasks.experiment_02.budget import PLATFORM_UNIT
from nanolab.tasks.experiment_02.platforms import FIRST_RESPONSE, WORKDIR
from nanolab.workspace.paths import discover_tool_root

from .fakes import RecordingProvider, Result, vm

PAYLOAD = b'{"size":128,"text":"A"}'
PROBE = {"started": 1.0, "api": 2.0, "registered": 3.0, "first": 4.0, "attempts": 1}
BUDGET = Budget(cpus=2, memoryMiB=2048)
ROOT = PurePosixPath("/opt/nanolab-e02/baseline/0123456789abcdef")
IMAGE = "127.0.0.1:5000/qr-baseline:0123456789abcdef"


def _protocol(concurrency: int = 3):
    scenario = discover_tool_root() / "scenarios" / "experiment-02-pilot.yaml"
    config = ScenarioConfig.model_validate(yaml.safe_load(scenario.read_text()))
    assert config.experiment_02 is not None
    return config.experiment_02.model_copy(update={"fixed_concurrency": concurrency})


def _spec(platform, tmp_path):
    def handler(argv, _env):
        if argv[0] == "python3":
            provider.files[str(FIRST_RESPONSE)] = b'"..."'
            return Result(0, json.dumps(PROBE))
        return None

    provider = RecordingProvider(handler=handler)
    platform.start(
        vm(provider), protocol=_protocol(), payload=PAYLOAD, evidence=tmp_path
    )
    return json.loads(provider.files[str(WORKDIR / "spec.json")])


def _unit(command: list[str]) -> list[str]:
    assert command[command.index("--unit") + 1] == PLATFORM_UNIT
    return command[command.index("--") + 1 :]


def test_tinyfaas_runs_its_manager_in_the_unit_on_the_entry_port(tmp_path):
    spec = _spec(TinyFaas(root=ROOT), tmp_path)
    [manager] = spec["start"]
    unit = " ".join(manager)
    assert "http_PORT=30080" in unit
    # rproxy at eff00d0 exits with fewer than two protocols
    # (cmd/rproxy/main.go:22), so CoAP and gRPC keep their defaults.
    assert "_PORT=-1" not in unit
    _unit(manager)
    target = TinyFaas(root=ROOT).target("10.0.0.4")
    assert (target.url, target.extractor) == ("http://10.0.0.4:30080/qr", "json-base64")
    assert TinyFaas(root=ROOT).host_commands


def test_serverledge_registers_a_custom_image_with_etcd_in_the_budget(tmp_path):
    spec = _spec(Serverledge(budget=BUDGET, root=ROOT, image=IMAGE), tmp_path)
    started = " ".join(" ".join(command) for command in spec["start"])
    assert "etcd:v3.5.13" in started
    assert "serverledge-conf.yaml" in started
    [create] = spec["register"]["commands"]
    assert "--runtime custom" in " ".join(create)
    target = Serverledge(budget=BUDGET, root=ROOT, image=IMAGE).target("10.0.0.4")
    assert (target.url, target.extractor, target.field) == (
        "http://10.0.0.4:30080/invoke/qr",
        "json-string-base64",
        "Result",
    )
    assert json.loads(
        Serverledge(budget=BUDGET, root=ROOT, image=IMAGE).body(PAYLOAD)
    ) == {"Params": json.loads(PAYLOAD)}


def test_fn_runs_its_server_and_creates_an_http_trigger(tmp_path):
    spec = _spec(Fn(root=ROOT, image=IMAGE), tmp_path)
    [server] = spec["start"]
    assert (
        "30080:8080" in server and "/var/run/docker.sock:/var/run/docker.sock" in server
    )
    assert any(part.startswith("fnproject/fnserver@sha256:16dedb58") for part in server)
    [register] = spec["register"]["commands"]
    assert 'ensure("triggers"' in register[-1]
    assert '"type": "http", "source": "/qr"' in register[-1]
    compile(register[-1], "fn-register", "exec")  # the generated script parses
    assert (
        Fn(root=ROOT, image=IMAGE).target("10.0.0.4").url
        == "http://10.0.0.4:30080/t/e02/qr"
    )


def test_serverledge_and_fn_register_the_installed_image(tmp_path):
    # install_baselines pushes one tag per identity: the function must name
    # that tag, not a default no installation pushes.
    image = "127.0.0.1:5000/qr-{}:0123456789abcdef"
    serverledge = _spec(
        Serverledge(budget=BUDGET, root=ROOT, image=image.format("serverledge")),
        tmp_path / "s",
    )
    [create] = serverledge["register"]["commands"]
    assert create[create.index("--custom_image") + 1] == image.format("serverledge")
    fn = _spec(Fn(root=ROOT, image=image.format("fn")), tmp_path / "f")
    [register] = fn["register"]["commands"]
    assert f'"image": "{image.format("fn")}"' in register[-1]


def test_concurrency_knobs_follow_the_protocol(tmp_path):
    tinyfaas = _spec(TinyFaas(root=ROOT), tmp_path / "t")
    assert '"threads"] = 3' in tinyfaas["register"]["commands"][0][-1]
    serverledge = _spec(
        Serverledge(budget=BUDGET, root=ROOT, image=IMAGE), tmp_path / "s"
    )
    assert serverledge["register"]["commands"][0][-2:] == ["--max_concurrency", "3"]


def test_serverledge_pool_matches_the_budget(tmp_path):
    spec = _spec(
        Serverledge(budget=Budget(cpus=1.5, memoryMiB=1024), root=ROOT, image=IMAGE),
        tmp_path,
    )
    started = " ".join(" ".join(command) for command in spec["start"])
    assert "memory: 1024" in started
    assert "cpus: 1.5" in started


def test_tinyfaas_never_uploads_over_a_failed_upload(tmp_path):
    # A failed upload can leave handlers tinyFaaS no longer tracks; uploading
    # again would measure them too, so the retry fails instead.
    [command] = _spec(TinyFaas(root=ROOT), tmp_path)["register"]["commands"]
    calls = tmp_path / "calls"
    tools = tmp_path / "bin"
    tools.mkdir()
    for tool, out in (("docker", "abc123"), ("curl", ""), ("python3", "{}")):
        (tools / tool).write_text(
            f'#!/bin/sh\necho {tool} "$@" >> {calls}\necho {out}\n'
        )
        (tools / tool).chmod(0o755)
    done = subprocess.run(
        command,
        env={**os.environ, "PATH": f"{tools}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "curl" not in calls.read_text()
    assert "label=tinyfaas-function=qr" in calls.read_text()
