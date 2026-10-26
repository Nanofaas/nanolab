"""Experiment 01 accepts only a coherent protocol for one function on k8s."""

import copy
import hashlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nanolab.config.experiment_01 import Experiment01Protocol, experiment_01_payload
from nanolab.config.scenario import ScenarioConfig
from nanolab.workspace.paths import discover_tool_root

SCENARIOS = discover_tool_root() / "scenarios"


def _pilot() -> dict:
    return yaml.safe_load((SCENARIOS / "experiment-01-pilot.yaml").read_text())


def _protocol(**changes: object) -> dict:
    protocol = copy.deepcopy(_pilot()["experiment01"])
    protocol.update(changes)
    return protocol


GENERATOR = {
    "maxRateDeviation": 0.05,
    "maxDroppedFraction": 0.01,
    "windowSeconds": 10,
    "maxStartDelaySeconds": 2,
}


@pytest.mark.parametrize("name", ["experiment-01-pilot.yaml", "experiment-01.yaml"])
def test_shipped_scenarios_load_and_their_payload_hash_matches(name):
    config = ScenarioConfig.model_validate(
        yaml.safe_load((SCENARIOS / name).read_text())
    )
    assert config.experiment_01 is not None
    raw = experiment_01_payload(config.experiment_01, SCENARIOS / "payloads")
    assert raw == b'{"size":1024,"text":"' + b"A" * 1024 + b'"}\n'


def test_campaign_is_frozen_and_pilot_is_not():
    campaign = yaml.safe_load((SCENARIOS / "experiment-01.yaml").read_text())
    assert campaign["experiment01"]["status"] == "frozen"
    assert _pilot()["experiment01"]["status"] == "pilot"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"steps": [{"rate": 50, "durationSeconds": 60}] * 2}, "increasing"),
        ({"steps": [{"rate": 0, "durationSeconds": 60}]}, "greater than 0"),
        ({"steps": [{"rate": 25.5, "durationSeconds": 60}]}, "integer"),
        ({"warmup": {"rate": 0.5, "durationSeconds": 60}}, "integer"),
        ({"steps": [{"rate": 10, "durationSeconds": 0}]}, "greater than 0"),
        ({"steps": [{"rate": float("inf"), "durationSeconds": 60}]}, "integer"),
        ({"steps": []}, "at least 1"),
        ({"gracefulStopSeconds": 39}, "gracefulStopSeconds"),
        ({"drainSeconds": 30}, "drainSeconds"),
        ({"preAllocatedVUs": 7000}, "preAllocatedVUs"),
        ({"status": "frozen"}, "frozen"),
        ({"generator": {**GENERATOR, "windowSeconds": 61}}, "windowSeconds"),
        ({"generator": {**GENERATOR, "maxRateDeviation": 1.0}}, "less than 1"),
        ({"generator": {**GENERATOR, "maxStartDelaySeconds": -1}}, "greater than"),
        ({"payloadFile": "../x.json"}, "basename"),
        ({"unknown": 1}, "Extra inputs"),
    ],
)
def test_incoherent_protocols_are_rejected(changes, message):
    with pytest.raises(ValidationError, match=message):
        Experiment01Protocol.model_validate(_protocol(**changes))


def test_frozen_protocol_with_generator_thresholds_is_accepted():
    protocol = Experiment01Protocol.model_validate(
        _protocol(status="frozen", generator=GENERATOR)
    )
    assert protocol.generator is not None


def test_phases_are_back_to_back_from_the_idle_origin():
    protocol = Experiment01Protocol.model_validate(_protocol())
    phases = protocol.phases()
    assert [phase["name"] for phase in phases] == [
        "idle",
        "warmup",
        "step-01",
        "step-02",
        "step-03",
        "step-04",
        "drain",
    ]
    assert [phase["startSeconds"] for phase in phases] == [
        0,
        60,
        120,
        180,
        240,
        300,
        360,
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"functions": ["qr-code-rust", "qr-code-go"]},
        {"functions": ["qr-code-go"]},
        {"backend": "container"},
        {"experiment01": None},
        {"loadScale": 2.0},
        {"concurrencyControl": True},
    ],
)
def test_scenario_contract_rejects_other_shapes(changes):
    scenario = {**_pilot(), **changes}
    if scenario["experiment01"] is None:
        del scenario["experiment01"]
    with pytest.raises(ValidationError):
        ScenarioConfig.model_validate(scenario)


def test_experiment_block_belongs_only_to_its_profile():
    scenario = {
        **_pilot(),
        "loadProfile": "comparison",
        "functions": ["word-stats-java", "word-stats-javascript"],
    }
    del scenario["resources"]
    with pytest.raises(ValidationError, match="experiment01"):
        ScenarioConfig.model_validate(scenario)


def test_resources_must_be_explicit_for_both_containers():
    scenario = _pilot()
    del scenario["resources"]["qr-code-rust"]["requests"]
    with pytest.raises(ValidationError, match="qr-code-rust"):
        ScenarioConfig.model_validate(scenario)


def test_payload_with_another_hash_is_refused(tmp_path: Path):
    protocol = Experiment01Protocol.model_validate(_protocol())
    (tmp_path / protocol.payload_file).write_bytes(b'{"size":1024,"text":"B"}')
    with pytest.raises(ValueError, match="sha256"):
        experiment_01_payload(protocol, tmp_path)


def test_payload_must_be_a_qr_request(tmp_path: Path):
    raw = b'{"size":1024,"text":"A","extra":1}'
    protocol = Experiment01Protocol.model_validate(
        _protocol(payloadSha256=hashlib.sha256(raw).hexdigest())
    )
    (tmp_path / protocol.payload_file).write_bytes(raw)
    with pytest.raises(ValueError, match="qr-code"):
        experiment_01_payload(protocol, tmp_path)
