"""FunLess and Spin: the Wasm baselines, started in the budget, secrets kept out."""

import json
from pathlib import PurePosixPath

import yaml

from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.experiment_02.baselines import FunLess, Spin
from nanolab.tasks.experiment_02.budget import PLATFORM_UNIT
from nanolab.tasks.experiment_02.generator import generator_environment
from nanolab.tasks.experiment_02.platforms import FIRST_RESPONSE, WORKDIR
from nanolab.workspace.paths import discover_tool_root

from .fakes import RecordingProvider, Result, vm

ROOT = PurePosixPath("/opt/nanolab-e02/baseline/0123456789abcdef")

PAYLOAD = b'{"size":128,"text":"A"}'
TOKEN = "SFMyNTY.secret-guest-token"
PROBE = {"started": 1.0, "api": 2.0, "registered": 3.0, "first": 4.0, "attempts": 1}


def _protocol():
    scenario = discover_tool_root() / "scenarios" / "experiment-02-pilot.yaml"
    config = ScenarioConfig.model_validate(yaml.safe_load(scenario.read_text()))
    assert config.experiment_02 is not None
    return config.experiment_02


def _start(platform, tmp_path):
    def handler(argv, _env):
        if argv[0] == "python3":
            provider.files[str(FIRST_RESPONSE)] = b'{"data": "..."}'
            return Result(0, json.dumps(PROBE))
        if argv[:2] == ("sh", "-c") and "/tmp/funless/tokens" in argv[-1]:
            return Result(0, json.dumps({"Authorization": f"Bearer {TOKEN}"}))
        return None

    provider = RecordingProvider(handler=handler)
    stack = vm(provider)
    timings = platform.start(
        stack, protocol=_protocol(), payload=PAYLOAD, evidence=tmp_path
    )
    spec = json.loads(provider.files[str(WORKDIR / "spec.json")])
    return provider, stack, timings, spec


def test_funless_starts_its_compose_and_registers_through_the_api(tmp_path):
    _provider, stack, timings, spec = _start(FunLess(root=ROOT), tmp_path)
    assert timings is not None
    [up] = spec["start"]
    assert "compose" in up[-1] and "-p e02" in up[-1] and "up -d" in up[-1]
    module, function = spec["register"]["commands"]
    assert '"module"' in module[-1] and "/v1/fn" in module[-1]
    assert "name=qr" in function[-1] and "qr_funless.wasm" in function[-1]
    target = FunLess(root=ROOT).target("10.0.0.4")
    assert target.url == "http://10.0.0.4:30080/v1/fn/e02/qr"
    assert (target.extractor, target.field) == ("json-base64", "data")
    assert json.loads(FunLess(root=ROOT).body(PAYLOAD)) == {"args": json.loads(PAYLOAD)}
    assert FunLess(root=ROOT).secret_headers(stack) == {
        "Authorization": f"Bearer {TOKEN}"
    }


def test_funless_token_never_reaches_evidence(tmp_path):
    _provider, stack, _timings, spec = _start(FunLess(root=ROOT), tmp_path)
    environment = generator_environment(
        _protocol(),
        payload=PAYLOAD,
        body=FunLess(root=ROOT).body(PAYLOAD),
        target=FunLess(root=ROOT).target("10.0.0.4"),
        secret_headers=FunLess(root=ROOT).secret_headers(stack),
    )
    # The generator gets the token; nothing written to the evidence does.
    assert TOKEN in environment["EXPERIMENT_TARGET"]
    assert spec["invoke"]["headersCommand"]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(), path


def test_spin_runs_in_the_platform_unit_on_the_entry_port(tmp_path):
    _provider, _stack, _timings, spec = _start(Spin(root=ROOT), tmp_path)
    [unit] = spec["start"]
    assert unit[unit.index("--unit") + 1] == PLATFORM_UNIT
    assert unit[unit.index("--") + 1 :][1:5] == [
        "up",
        "--listen",
        "0.0.0.0:30080",
        "-f",
    ]
    assert spec["register"] is None
    assert Spin(root=ROOT).host_commands == ("spin",)
    target = Spin(root=ROOT).target("10.0.0.4")
    assert (target.url, target.extractor, target.field) == (
        "http://10.0.0.4:30080/",
        "json-base64",
        None,
    )
