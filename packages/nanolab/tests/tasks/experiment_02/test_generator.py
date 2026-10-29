"""The generator gets one protocol and one target; its ledger always comes back."""

import json
from pathlib import Path, PurePosixPath

import pytest
import yaml

from nanolab.config.experiment_01 import experiment_01_payload
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.experiment_02.generator import (
    K6Target,
    generator_environment,
    run_generator,
)
from nanolab.workspace.paths import bundled_assets_root, discover_tool_root

from .fakes import RecordingProvider, Result, vm

SCENARIOS = discover_tool_root() / "scenarios"
SCRIPT = bundled_assets_root() / "k6" / "experiment-01.js"
REMOTE = PurePosixPath("/home/azureuser/e02/attempt-1")


def _protocol():
    config = ScenarioConfig.model_validate(
        yaml.safe_load((SCENARIOS / "experiment-02-pilot.yaml").read_text())
    )
    assert config.experiment_02 is not None
    return config.experiment_02


def test_generator_environment_serializes_protocol_target_and_body():
    protocol = _protocol()
    payload = experiment_01_payload(protocol, SCENARIOS / "payloads")
    target = K6Target(
        url="http://10.0.0.4:9000/invoke",
        headers={"Content-Type": "application/json"},
        extractor="json-base64",
        id_header="X-Execution-Id",
    )
    env = generator_environment(
        protocol, payload=payload, body=b'{"input":{}}', target=target
    )
    # Experiment 01's two keys, in experiment 01's format, plus the target.
    assert set(env) == {"E01_PROTOCOL", "E01_PAYLOAD", "EXPERIMENT_TARGET"}
    assert env["E01_PAYLOAD"] == '{"input":{}}'
    assert json.loads(env["EXPERIMENT_TARGET"]) == {
        "url": "http://10.0.0.4:9000/invoke",
        "headers": {"Content-Type": "application/json"},
        "extractor": "json-base64",
        "field": None,
        "idHeader": "X-Execution-Id",
        "pngSha256": protocol.png_sha256,
    }
    sent = json.loads(env["E01_PROTOCOL"])
    assert set(sent) == {
        "phases",
        "httpTimeoutSeconds",
        "gracefulStopSeconds",
        "preAllocatedVUs",
        "maxVUs",
        "size",
    }
    assert sent["size"] == 1024
    assert [phase["name"] for phase in sent["phases"]] == [
        "idle",
        "warmup",
        "step-01",
        "step-02",
        "step-03",
        "step-04",
        "drain",
    ]
    assert sent["httpTimeoutSeconds"] == 30
    assert sent["gracefulStopSeconds"] == 40


def test_run_generator_fetches_events_even_when_k6_fails(tmp_path: Path):
    events = str(REMOTE / "k6-events.jsonl")

    def k6(argv: tuple[str, ...], env: dict[str, str] | None) -> Result | None:
        if argv[:2] == ("k6", "run"):
            assert env == {"K6_CONSOLE_OUTPUT": events}
            provider.files[events] = b'{"t":"origin","ts":1}\n'
            return Result(99, "", "thresholds crossed")
        return None

    provider = RecordingProvider(handler=k6)
    code = run_generator(
        vm(provider, "loadgen"),
        script=SCRIPT,
        env={"E01_PAYLOAD": "{}"},
        remote_dir=REMOTE,
        local_dir=tmp_path,
    )
    assert code == 99
    assert (tmp_path / "k6-events.jsonl").read_text() == '{"t":"origin","ts":1}\n'
    assert not (tmp_path / "k6-summary.json").exists()
    assert (tmp_path / "k6-stderr.txt").read_text() == "thresholds crossed"
    k6_run = next(argv for argv in provider.commands() if argv[:2] == ("k6", "run"))
    assert k6_run[-1] == str(REMOTE / "experiment-01.js")
    assert k6_run[-3:-1] == ("-e", "E01_PAYLOAD={}")


def test_run_generator_refuses_a_script_that_arrived_changed(tmp_path: Path):
    provider = RecordingProvider()

    def corrupt(_request, *, source, destination):
        provider.files[destination] = b"changed"
        return Result()

    provider.transfer_to = corrupt  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="differs"):
        run_generator(
            vm(provider, "loadgen"),
            script=SCRIPT,
            env={},
            remote_dir=REMOTE,
            local_dir=tmp_path,
        )
    assert not any(argv[:2] == ("k6", "run") for argv in provider.commands())
