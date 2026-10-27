"""nanoFaaS P0/P1 and the function alone, started, probed and removed on Docker."""

import json

import pytest
import yaml

from nanolab.comparison.profiles import EXPERIMENT_02_MODULES
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.experiment_02.platforms import (
    FIRST_RESPONSE,
    WORKDIR,
    DirectReference,
    NanofaasPlatform,
    _record_containers,
    leftover_containers,
)
from nanolab.workspace.paths import discover_tool_root

from .fakes import RecordingProvider, Result, vm

PAYLOAD = b'{"size":128,"text":"A"}\n'
SPEC = str(WORKDIR / "spec.json")


def _protocol():
    config = ScenarioConfig.model_validate(
        yaml.safe_load(
            (
                discover_tool_root() / "scenarios" / "experiment-02-pilot.yaml"
            ).read_text()
        )
    )
    assert config.experiment_02 is not None
    return config.experiment_02


def _p1() -> NanofaasPlatform:
    return NanofaasPlatform(
        profile="P1",
        control_plane_image="registry/control-plane:p1",
        function_image="registry/rust-qr-code:e02",
        modules=EXPERIMENT_02_MODULES["P1"],
        concurrency=2,
        queue_capacity=200,
        timeout_ms=30000,
    )


def _p0() -> NanofaasPlatform:
    return NanofaasPlatform(
        profile="P0",
        control_plane_image="registry/control-plane:p0",
        function_image="registry/rust-qr-code:e02",
        modules=EXPERIMENT_02_MODULES["P0"],
        concurrency=2,
        queue_capacity=200,
        timeout_ms=30000,
    )


PROBE = {
    "started": 100.0,
    "api": 100.5,
    "registered": 101.0,
    "first": 102.25,
    "pngSha256": "ab" * 32,
    "attempts": 1,
    "intervalSeconds": 0.5,
}


def _start(platform, tmp_path, probe=PROBE):
    def handler(argv, _env):
        if argv[0] == "python3":
            if "first" in probe:
                provider.files[str(FIRST_RESPONSE)] = b'{"output": "..."}'
            return Result(0, json.dumps(probe))
        return None

    provider = RecordingProvider(handler=handler)
    timings = platform.start(
        vm(provider), protocol=_protocol(), payload=PAYLOAD, evidence=tmp_path
    )
    return provider, timings, json.loads(provider.files[SPEC])


def test_p1_runs_the_docker_provider_and_registers_a_deployment_function(tmp_path):
    _provider, _timings, spec = _start(_p1(), tmp_path)
    [control_plane] = spec["start"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in control_plane
    assert "NANOFAAS_DEPLOYMENT_DEFAULT_BACKEND=container-local" in control_plane
    assert "30080:8080" in control_plane
    assert "30081:8081" in control_plane
    assert control_plane[-1] == "registry/control-plane:p1"
    assert spec["register"]["body"] == {
        "name": "qr-code-rust",
        "image": "registry/rust-qr-code:e02",
        "executionMode": "DEPLOYMENT",
        "concurrency": 2,
        "queueSize": 200,
        "maxRetries": 0,
        "timeoutMs": 30000,
    }
    assert spec["invoke"]["url"] == (
        "http://127.0.0.1:30080/v1/functions/qr-code-rust:invoke"
    )
    assert spec["intervalSeconds"] == 0.5
    assert _p1().metrics_url == "http://127.0.0.1:30081/actuator/prometheus"


def test_p0_registers_an_external_endpoint_without_the_docker_socket(tmp_path):
    _provider, _timings, spec = _start(_p0(), tmp_path)
    function, control_plane = spec["start"]
    assert function[-1] == "registry/rust-qr-code:e02"
    assert "PORT=8080" in function
    assert not any("docker.sock" in part for part in control_plane)
    assert spec["register"]["body"]["executionMode"] == "EXTERNAL"
    assert spec["register"]["body"]["endpointUrl"] == "http://e02-qr-code:8080/invoke"
    assert "queueSize" not in spec["register"]["body"]


def test_images_are_present_before_the_clock_starts(tmp_path):
    provider, _timings, _spec = _start(_p1(), tmp_path)
    commands = provider.commands()
    clock = next(i for i, argv in enumerate(commands) if argv[0] == "python3")
    pulls = [
        index
        for index, argv in enumerate(commands)
        if argv[0] == "sh" and "docker pull" in argv[-1]
    ]
    assert len(pulls) == 2
    assert max(pulls) < clock


def test_direct_reference_wraps_the_payload_and_numbers_each_request(tmp_path):
    direct = DirectReference("registry/rust-qr-code:e02")
    assert json.loads(direct.body(PAYLOAD)) == {"input": {"size": 128, "text": "A"}}
    target = direct.target("10.0.0.4")
    assert target.url == "http://10.0.0.4:30080/invoke"
    assert (target.extractor, target.field) == ("json-base64", None)
    # One id per request: the runtime refuses a second active execution of an id.
    assert target.id_header == "X-Execution-Id"
    assert direct.metrics_url is None
    _provider, timings, spec = _start(direct, tmp_path, {**PROBE, "registered": 0})
    [function] = spec["start"]
    assert "30080:8080" in function
    assert not any(part.startswith("EXECUTION_ID=") for part in function)
    assert spec["register"] is None
    assert spec["invoke"]["idHeader"] == "X-Execution-Id"
    assert timings is not None
    assert timings["registeredSeconds"] == timings["apiReadySeconds"]


def test_readiness_reports_seconds_from_the_start_measured_on_the_vm(tmp_path):
    _provider, timings, spec = _start(_p1(), tmp_path)
    assert timings == {
        "apiReadySeconds": 0.5,
        "registeredSeconds": 1.0,
        "firstCorrectSeconds": 2.25,
    }
    recorded = json.loads((tmp_path / "readiness.json").read_text())
    assert recorded["pngSha256"] == "ab" * 32
    assert recorded["probe"]["attempts"] == 1
    assert (tmp_path / "first-response.body").is_file()
    assert spec["timeoutSeconds"] == _protocol().readiness_timeout_seconds


@pytest.mark.parametrize(
    "probe",
    [
        {"started": 100.0, "api": 100.5, "timeout": "registered", "last": {}},
        {"started": 100.0, "failed": "start", "code": 125, "stderr": "conflict"},
    ],
)
def test_no_correct_answer_is_none_with_the_reason_recorded(tmp_path, probe):
    _provider, timings, _spec = _start(_p1(), tmp_path, probe)
    assert timings is None
    recorded = json.loads((tmp_path / "readiness.json").read_text())
    assert recorded["firstCorrectSeconds"] is None
    assert recorded["probe"] == probe
    assert not (tmp_path / "first-response.body").exists()


def test_teardown_removes_every_container_but_the_registry():
    running = ["e02-control-plane", "nanofaas-e2e-registry", "qr-code-1"]

    def handler(argv, _env):
        if argv[:3] == ("docker", "ps", "-a"):
            return Result(0, "".join(f"{name}\n" for name in running))
        if argv[:3] == ("docker", "rm", "-f"):
            running[:] = [name for name in running if name not in argv[3:]]
        return None

    provider = RecordingProvider(handler=handler)
    stack = vm(provider)
    assert leftover_containers(stack) == ["e02-control-plane", "qr-code-1"]
    _p1().teardown(stack)
    assert ("docker", "rm", "-f", "e02-control-plane", "qr-code-1") in (
        provider.commands()
    )


def test_record_refuses_a_control_plane_with_unexpected_modules(tmp_path):
    def metadata(modules: list[str]):
        def handler(argv, _env):
            if argv[0] == "curl" and argv[-1].endswith("/modules/build-metadata"):
                return Result(
                    0, json.dumps({"modules": modules, "build": {"type": "jvm"}})
                )
            return None

        return RecordingProvider(handler=handler)

    with pytest.raises(ValueError, match="modules"):
        _p1().record(vm(metadata(["build-metadata"])), tmp_path)
    _p1().record(vm(metadata(sorted(EXPERIMENT_02_MODULES["P1"]))), tmp_path)
    assert json.loads((tmp_path / "build-metadata.json").read_text())["modules"]


def test_nanofaas_is_sent_the_payload_as_invocation_input(tmp_path):
    # The control plane's InvocationRequest: {"input", "metadata", "headers"};
    # a bare payload is a 400 "Failed to read HTTP message".
    assert json.loads(_p1().body(PAYLOAD)) == {"input": {"size": 128, "text": "A"}}
    _provider, _timings, spec = _start(_p0(), tmp_path)
    assert json.loads(spec["invoke"]["body"]) == {"input": {"size": 128, "text": "A"}}


def test_direct_reference_admits_the_protocols_fixed_concurrency(tmp_path):
    # Without a cap the runtime admits 32 handlers and collapses under contention
    # (notes/evidence.md, 27 October 2026); nanoFaaS P1 admits fixedConcurrency.
    direct = DirectReference("registry/rust-qr-code:e02")
    protocol = _protocol().model_copy(update={"fixed_concurrency": 3})

    def handler(argv, _env):
        if argv[0] == "python3":
            return Result(0, json.dumps({"started": 1.0, "timeout": "api"}))
        return None

    provider = RecordingProvider(handler=handler)
    direct.start(vm(provider), protocol=protocol, payload=PAYLOAD, evidence=tmp_path)
    [function] = json.loads(provider.files[SPEC])["start"]
    assert "NANOFAAS_MAX_CONCURRENT_HANDLERS=3" in function
    assert function[-1] == "registry/rust-qr-code:e02"


def test_teardown_stops_units_and_compose_projects():
    from nanolab.tasks.experiment_02.budget import PLATFORM_UNIT
    from nanolab.tasks.experiment_02.platforms import baseline_teardown

    provider = RecordingProvider()
    baseline_teardown(vm(provider))
    commands = provider.commands()
    assert ("sudo", "systemctl", "stop", PLATFORM_UNIT) in commands
    assert ("sudo", "docker", "compose", "-p", "e02", "down", "-v") in commands
    # tinyFaaS creates a network per function; leftovers exhaust Docker's pools.
    assert ("docker", "network", "prune", "-f") in commands
    assert commands[-1] == ("docker", "network", "prune", "-f")


def test_teardown_waits_out_containers_a_platform_is_already_removing(monkeypatch):
    # Fn removes its own function containers while the teardown runs.
    from nanolab.tasks.experiment_02 import platforms

    monkeypatch.setattr(platforms.time, "sleep", lambda _seconds: None)
    listings = iter(["e02-fnserver\nfn-1\n", "e02-fnserver\nfn-1\n", "fn-1\n", ""])

    def handler(argv, _env):
        if argv[:3] == ("docker", "ps", "-a"):
            return Result(0, next(listings, ""))
        if argv[:3] == ("docker", "rm", "-f"):
            return Result(1, "", "removal of container fn-1 is already in progress")
        return None

    platforms.baseline_teardown(vm(RecordingProvider(handler=handler)))


def test_teardown_fails_when_containers_never_go(monkeypatch):
    from nanolab.tasks.experiment_02 import platforms

    monkeypatch.setattr(platforms.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(platforms, "_REMOVAL_WAIT_SECONDS", 0.0)

    def handler(argv, _env):
        if argv[:3] == ("docker", "ps", "-a"):
            return Result(0, "fn-1\n")
        return None

    with pytest.raises(RuntimeError, match="fn-1"):
        platforms.baseline_teardown(vm(RecordingProvider(handler=handler)))


def test_teardown_removes_containers_created_while_it_runs(monkeypatch):
    # fnserver still creates function containers while the first removal runs.
    from nanolab.tasks.experiment_02 import platforms

    monkeypatch.setattr(platforms.time, "sleep", lambda _seconds: None)
    running = ["e02-fnserver", "fn-1"]
    removals = 0

    def handler(argv, _env):
        nonlocal removals
        if argv[:3] == ("docker", "ps", "-a"):
            return Result(0, "".join(f"{name}\n" for name in running))
        if argv[:3] == ("docker", "rm", "-f"):
            removals += 1
            running[:] = [name for name in running if name not in argv[3:]]
            if removals == 1:
                running.append("fn-2")  # created during the first removal
        return None

    platforms.baseline_teardown(vm(RecordingProvider(handler=handler)))
    assert running == []


def test_recorded_containers_leave_out_their_environment(tmp_path):
    # FunLess's SECRET_KEY_BASE signs its tokens: no environment is evidence.
    inspected = [{"Name": "/e02-core-1", "Config": {"Env": ["SECRET_KEY_BASE=s3"]}}]

    def handler(argv, _env):
        if argv[:3] == ("docker", "ps", "-a"):
            return Result(0, "e02-core-1\n")
        if argv[:2] == ("docker", "inspect"):
            return Result(0, json.dumps(inspected))
        return None

    _record_containers(vm(RecordingProvider(handler=handler)), tmp_path)
    [recorded] = json.loads((tmp_path / "containers.json").read_text())
    assert recorded["Name"] == "/e02-core-1"
    assert "s3" not in (tmp_path / "containers.json").read_text()
