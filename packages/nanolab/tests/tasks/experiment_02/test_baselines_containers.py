"""tinyFaaS, Serverledge and Fn: started in the budget, configured from the protocol."""

import json

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
    spec = _spec(TinyFaas(), tmp_path)
    [manager] = spec["start"]
    unit = " ".join(manager)
    for variable in ("http_PORT=30080", "coap_PORT=-1", "grpc_PORT=-1"):
        assert variable in unit
    _unit(manager)
    target = TinyFaas().target("10.0.0.4")
    assert (target.url, target.extractor) == ("http://10.0.0.4:30080/qr", "json-base64")
    assert TinyFaas().host_commands


def test_serverledge_registers_a_custom_image_with_etcd_in_the_budget(tmp_path):
    spec = _spec(Serverledge(budget=BUDGET), tmp_path)
    started = " ".join(" ".join(command) for command in spec["start"])
    assert "etcd:v3.5.13" in started
    assert "serverledge-conf.yaml" in started
    [create] = spec["register"]["commands"]
    assert "--runtime custom" in " ".join(create)
    target = Serverledge(budget=BUDGET).target("10.0.0.4")
    assert (target.url, target.extractor, target.field) == (
        "http://10.0.0.4:30080/invoke/qr",
        "json-string-base64",
        "Result",
    )
    assert json.loads(Serverledge(budget=BUDGET).body(PAYLOAD)) == {
        "Params": json.loads(PAYLOAD)
    }


def test_fn_runs_its_server_and_creates_an_http_trigger(tmp_path):
    spec = _spec(Fn(), tmp_path)
    [server] = spec["start"]
    assert (
        "30080:8080" in server and "/var/run/docker.sock:/var/run/docker.sock" in server
    )
    assert any(part.startswith("fnproject/fnserver@sha256:16dedb58") for part in server)
    [register] = spec["register"]["commands"]
    assert 'ensure("triggers"' in register[-1]
    assert '"type": "http", "source": "/qr"' in register[-1]
    compile(register[-1], "fn-register", "exec")  # the generated script parses
    assert Fn().target("10.0.0.4").url == "http://10.0.0.4:30080/t/e02/qr"


def test_concurrency_knobs_follow_the_protocol(tmp_path):
    tinyfaas = _spec(TinyFaas(), tmp_path / "t")
    assert '"threads"] = 3' in tinyfaas["register"]["commands"][0][-1]
    serverledge = _spec(Serverledge(budget=BUDGET), tmp_path / "s")
    assert serverledge["register"]["commands"][0][-2:] == ["--max_concurrency", "3"]


def test_serverledge_pool_matches_the_budget(tmp_path):
    spec = _spec(Serverledge(budget=Budget(cpus=1.5, memoryMiB=1024)), tmp_path)
    started = " ".join(" ".join(command) for command in spec["start"])
    assert "memory: 1024" in started
    assert "cpus: 1.5" in started
