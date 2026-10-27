"""One cell: sampler, budget, platform, generator, evidence, cleanup, outcome."""

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest
import yaml

from nanolab.comparison.experiment_01 import DIAGNOSTICS, LOAD_STARTED
from nanolab.comparison.experiment_02 import (
    CONFIGURATIONS_BY_KEY,
    SAMPLES,
    SCHEDULER,
    Experiment02Cell,
)
from nanolab.config.experiment_02 import Budget
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.experiment_02.budget import OBSERVER_SLICE
from nanolab.tasks.experiment_02.cell import CLEANUP_ERRORS, SAMPLER_UNIT, run_cell
from nanolab.tasks.experiment_02.generator import K6Target
from nanolab.tasks.experiment_02.vm import Vm
from nanolab.workspace.paths import bundled_assets_root, discover_tool_root

from .fakes import RecordingProvider, Result, vm

SCENARIOS = discover_tool_root() / "scenarios"
SCRIPT = bundled_assets_root() / "k6" / "experiment-01.js"
REMOTE = PurePosixPath("/home/azureuser/e02/cell/attempts/1")
SAMPLES_REMOTE = str(REMOTE / "cgroup-samples.jsonl")
SCHEDULER_REMOTE = str(REMOTE / "scheduler-samples.jsonl")
BUDGET = Budget(cpus=2, memoryMiB=2048)
CELL = Experiment02Cell(CONFIGURATIONS_BY_KEY["nanofaas-p1-jvm"], BUDGET, 1)
INSIDE = "201 java /nanolab.slice/nanolab-sut.slice/docker-ab.scope\n"
METRICS = "http://127.0.0.1:30081/actuator/prometheus"


def _protocol():
    config = ScenarioConfig.model_validate(
        yaml.safe_load((SCENARIOS / "experiment-02-pilot.yaml").read_text())
    )
    assert config.experiment_02 is not None
    return config.experiment_02


def _sample(ts: float) -> str:
    group = {
        "memoryCurrent": 100,
        "inactiveFile": 0,
        "cpuUsageUsec": int(ts * 1000),
        "nrThrottled": 0,
        "throttledUsec": 0,
        "oomKill": 0,
    }
    return json.dumps({"ts": ts, "cgroups": {".": group}})


@dataclass
class FakePlatform:
    """Records what the cell asks of it; readiness answers as configured."""

    ready: bool = True
    failure: Exception | None = None
    metrics_url: str | None = METRICS
    host_commands: tuple[str, ...] = ()
    calls: list[str] = field(default_factory=list)

    def start(self, vm: Vm, *, protocol, payload, evidence):
        self.calls.append("start")
        if self.failure is not None:
            raise self.failure
        if not self.ready:
            return None
        return {
            "apiReadySeconds": 1.0,
            "registeredSeconds": 2.0,
            "firstCorrectSeconds": 3.0,
        }

    def target(self, host: str) -> K6Target:
        return K6Target(f"http://{host}:30080/invoke", {}, "json-base64")

    def body(self, payload: bytes) -> bytes:
        return payload

    def secret_headers(self, vm: Vm) -> dict[str, str]:
        return {}

    def record(self, vm: Vm, evidence: Path) -> None:
        self.calls.append("record")

    def teardown(self, vm: Vm) -> None:
        self.calls.append("teardown")


@dataclass
class _State:
    started: bool = False
    sampling: bool = False


def _stack(
    *,
    budget: str = "200000 100000\n2147483648\n0\n",
    listing: str = INSIDE,
    leftovers: list[str] | None = None,
    oom: tuple[int, int] = (0, 0),
    after_fails: bool = False,
    restart_fails: bool = False,
    stale_sampler: bool = False,
    oom_killed: bool = False,
) -> RecordingProvider:
    remaining = list(leftovers or [])
    reads = iter(oom)
    state = _State()

    def handler(argv, _env):
        if argv[:3] == ("docker", "ps", "-a"):
            running = ["e02-control-plane"] if oom_killed and state.sampling else []
            names = ["nanofaas-e2e-registry", *remaining, *running]
            return Result(0, "\n".join(names) + "\n")
        if argv[:3] == ("docker", "rm", "-f"):
            remaining.clear()
            return None
        if argv[:4] == ("timeout", "30", "docker", "ps"):
            return Result(0, "CONTAINER ID   NAMES\n")
        if argv[:2] == ("systemctl", "is-active"):
            return Result(0 if stale_sampler else 3)
        if argv[0] == "sh" and "stat -c %i" in argv[-1]:
            if after_fails and state.started:
                return Result(1, "", "ssh: connection closed")
            state.started = True
            return Result(0, f"4242\noom 0\noom_kill {next(reads)}\n")
        if argv[:3] == ("sudo", "systemctl", "restart") and restart_fails:
            return Result(1, "", "Job for docker.service failed")
        if argv[0] == "cat":
            return Result(0, budget)
        if argv[0] == "sh" and "/proc/" in argv[-1]:
            return Result(0, listing)
        if argv[:4] == ("timeout", "30", "docker", "inspect"):
            return Result(0, f"/e02-control-plane {str(oom_killed).lower()}\n")
        if argv[:2] == ("sudo", "systemd-run"):
            state.sampling = True
        if argv[:3] == ("sudo", "systemctl", "stop") and state.sampling:
            provider.files[SAMPLES_REMOTE] = (
                "\n".join(_sample(t) for t in range(0, 100, 5)) + "\n"
            ).encode()
            if "--metrics-url" in next(
                cmd for cmd in provider.commands() if cmd[:2] == ("sudo", "systemd-run")
            ):
                provider.files[SCHEDULER_REMOTE] = b'{"status":"ok"}\n'
        return None

    provider = RecordingProvider(handler=handler)
    return provider


def _loadgen(code: int = 0, summary: bool = True) -> RecordingProvider:
    def handler(argv, _env):
        if argv[:2] == ("k6", "run"):
            provider.files[str(REMOTE / "k6-events.jsonl")] = b'{"t":"origin","ts":1}\n'
            if summary:
                provider.files[str(REMOTE / "k6-summary.json")] = b"{}"
            return Result(code)
        return None

    provider = RecordingProvider(handler=handler)
    return provider


def _run(stack, loadgen, platform, tmp_path):
    return run_cell(
        CELL,
        stack=vm(stack, "stack"),
        loadgen=vm(loadgen, "loadgen"),
        platform=platform,
        protocol=_protocol(),
        payload=b'{"size":1024,"text":"A"}',
        script=SCRIPT,
        run_dir=tmp_path,
        remote_root=REMOTE,
        host="20.0.0.4",
    )


def _index(commands, prefix):
    return next(i for i, argv in enumerate(commands) if argv[: len(prefix)] == prefix)


def _stopped_after_start(stack: RecordingProvider) -> bool:
    commands = stack.commands()
    start = _index(commands, ("sudo", "systemd-run"))
    return any(
        argv[:4] == ("sudo", "systemctl", "stop", SAMPLER_UNIT)
        for argv in commands[start:]
    )


def test_cell_measures_in_order(tmp_path):
    stack, loadgen, platform = _stack(), _loadgen(), FakePlatform()
    assert _run(stack, loadgen, platform, tmp_path) == "measured"
    commands = stack.commands()
    order = [
        next(i for i, argv in enumerate(commands) if "stat -c %i" in argv[-1]),
        _index(commands, ("sudo", "systemd-run")),
        _index(commands, ("sudo", "systemctl", "set-property")),
        _index(commands, ("sudo", "systemctl", "restart")),
        _index(commands, ("cat",)),
    ]
    assert order == sorted(order)
    assert platform.calls == ["start", "record", "teardown"]
    assert (tmp_path / LOAD_STARTED).is_file()
    assert (tmp_path / SAMPLES).is_file()
    assert (tmp_path / SCHEDULER).is_file()
    assert (tmp_path / "k6-events.jsonl").is_file()
    assert json.loads((tmp_path / "budget.json").read_text())["observed"] == {
        "cpus": 2.0,
        "memoryMiB": 2048,
    }
    sampler = commands[_index(commands, ("sudo", "systemd-run"))]
    assert sampler[sampler.index("--slice") + 1] == OBSERVER_SLICE
    assert sampler[sampler.index("--metrics-url") + 1] == METRICS
    assert SAMPLER_UNIT in sampler
    diagnostics = tmp_path / DIAGNOSTICS
    assert json.loads((diagnostics / "cgroup-before.json").read_text())["inode"]
    assert (diagnostics / "cgroup-after.json").is_file()
    assert not (diagnostics / CLEANUP_ERRORS).exists()


def test_direct_reference_has_no_scheduler_scrape(tmp_path):
    stack = _stack()
    platform = FakePlatform(metrics_url=None)
    assert _run(stack, _loadgen(), platform, tmp_path) == "measured"
    sampler = stack.commands()[_index(stack.commands(), ("sudo", "systemd-run"))]
    assert "--metrics-url" not in sampler
    assert not (tmp_path / SCHEDULER).exists()
    assert ("get", SCHEDULER_REMOTE) not in stack.calls


def test_readiness_timeout_is_startup_failed_with_diagnostics(tmp_path):
    stack, loadgen, platform = _stack(), _loadgen(), FakePlatform(ready=False)
    assert _run(stack, loadgen, platform, tmp_path) == "startup_failed"
    assert (tmp_path / DIAGNOSTICS / "error.txt").is_file()
    assert not any(argv[:2] == ("k6", "run") for argv in loadgen.commands())
    assert platform.calls[-1] == "teardown"
    assert not (tmp_path / LOAD_STARTED).exists()
    assert _stopped_after_start(stack)


def test_oom_kill_during_load_is_oom(tmp_path):
    stack = _stack(oom=(3, 4))
    assert _run(stack, _loadgen(), FakePlatform(), tmp_path) == "oom"
    oom = (tmp_path / DIAGNOSTICS / "oom.txt").read_text()
    assert "1 by the cgroup counter" in oom


def test_oom_before_readiness_is_oom(tmp_path):
    stack = _stack(oom=(3, 4))
    assert _run(stack, _loadgen(), FakePlatform(ready=False), tmp_path) == "oom"


def test_engine_oom_during_restart_is_oom(tmp_path):
    # Docker cannot restart, but the counter read over SSH proves the OOM.
    stack = _stack(oom=(3, 4), restart_fails=True)
    platform = FakePlatform()
    assert _run(stack, _loadgen(), platform, tmp_path) == "oom"
    assert "start" not in platform.calls


def test_oom_with_generator_error_is_oom(tmp_path):
    loadgen = _loadgen(code=107, summary=False)
    assert _run(_stack(oom=(0, 1)), loadgen, FakePlatform(), tmp_path) == "oom"


def test_an_oom_killed_container_is_oom_even_without_the_counter(tmp_path):
    assert _run(_stack(oom_killed=True), _loadgen(), FakePlatform(), tmp_path) == "oom"


def test_unknown_oom_evidence_stops_without_a_measured_receipt(tmp_path):
    stack = _stack(after_fails=True)

    def no_samples(_request, *, source, destination):
        return Result(1, "", "connection closed")

    stack.transfer_from = no_samples  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="OOM evidence is missing"):
        _run(stack, _loadgen(), FakePlatform(), tmp_path)
    errors = json.loads((tmp_path / DIAGNOSTICS / CLEANUP_ERRORS).read_text())
    assert {error["step"] for error in errors} >= {"cgroup-after", "fetch-samples"}


def test_budget_mismatch_stops_before_deploy(tmp_path):
    stack = _stack(budget="100000 100000\n2147483648\n0\n")
    platform = FakePlatform()
    with pytest.raises(ValueError, match="cpu1-mem2048"):
        _run(stack, _loadgen(), platform, tmp_path)
    assert platform.calls == ["teardown"]
    assert _stopped_after_start(stack)


def test_process_outside_the_slice_stops_before_load(tmp_path):
    outside = INSIDE + "101 containerd-shim /system.slice/containerd.service\n"
    loadgen, platform = _loadgen(), FakePlatform()
    with pytest.raises(ValueError, match="outside the measured slice"):
        _run(_stack(listing=outside), loadgen, platform, tmp_path)
    assert not any(argv[:2] == ("k6", "run") for argv in loadgen.commands())
    assert platform.calls[-1] == "teardown"


def test_leftover_containers_are_removed_before_deploy(tmp_path):
    stack = _stack(leftovers=["e02-control-plane"])
    assert _run(stack, _loadgen(), FakePlatform(), tmp_path) == "measured"
    commands = stack.commands()
    assert _index(commands, ("docker", "rm", "-f", "e02-control-plane")) < _index(
        commands, ("sudo", "systemctl", "set-property")
    )


def test_a_sampler_left_running_refuses_the_cell(tmp_path):
    platform = FakePlatform()
    with pytest.raises(ValueError, match="sampler"):
        _run(_stack(stale_sampler=True), _loadgen(), platform, tmp_path)
    assert platform.calls == []


@pytest.mark.parametrize(
    ("failure", "code", "error"),
    [
        (RuntimeError("deploy failed"), 0, "deploy failed"),
        (None, 105, "k6 exited 105"),
    ],
)
def test_sampler_stop_and_fetch_run_after_each_failure(tmp_path, failure, code, error):
    stack, platform = _stack(), FakePlatform(failure=failure)
    with pytest.raises(RuntimeError, match=error):
        _run(stack, _loadgen(code=code), platform, tmp_path)
    assert _stopped_after_start(stack)
    assert (tmp_path / SAMPLES).is_file()
    assert platform.calls[-1] == "teardown"


def test_nonzero_k6_without_oom_never_produces_measured(tmp_path):
    # k6 interrupted after writing a summary: the ledger may be truncated.
    with pytest.raises(RuntimeError, match="k6 exited 105"):
        _run(_stack(), _loadgen(code=105, summary=True), FakePlatform(), tmp_path)


def test_cleanup_errors_preserve_the_original_exception(tmp_path):
    stack = _stack()
    original = stack.transfer_from

    def flaky(request, *, source, destination):
        if source == SAMPLES_REMOTE:
            return Result(1, "", "scp: broken pipe")
        return original(request, source=source, destination=destination)

    stack.transfer_from = flaky  # type: ignore[method-assign]
    platform = FakePlatform()
    with pytest.raises(RuntimeError, match="k6 exited 99"):
        _run(stack, _loadgen(code=99), platform, tmp_path)
    assert platform.calls[-1] == "teardown"
    errors = json.loads((tmp_path / DIAGNOSTICS / CLEANUP_ERRORS).read_text())
    assert [error["step"] for error in errors] == ["fetch-samples"]
