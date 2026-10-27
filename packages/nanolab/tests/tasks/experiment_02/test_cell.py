"""One cell: budget, engine, platform, generator, samples, teardown, outcome."""

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest
import yaml

from nanolab.comparison.experiment_01 import DIAGNOSTICS, LOAD_STARTED
from nanolab.comparison.experiment_02 import (
    CONFIGURATIONS_BY_KEY,
    SAMPLES,
    Experiment02Cell,
)
from nanolab.config.experiment_02 import Budget
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.experiment_02.cell import SAMPLER_UNIT, run_cell
from nanolab.tasks.experiment_02.generator import K6Target
from nanolab.tasks.experiment_02.vm import Vm
from nanolab.workspace.paths import bundled_assets_root, discover_tool_root

from .fakes import RecordingProvider, Result, vm

SCENARIOS = discover_tool_root() / "scenarios"
SCRIPT = bundled_assets_root() / "k6" / "experiment-01.js"
REMOTE = PurePosixPath("/home/azureuser/e02/cell/attempts/1")
SAMPLES_REMOTE = str(REMOTE / "cgroup-samples.jsonl")
BUDGET = Budget(cpus=2, memoryMiB=2048)
CELL = Experiment02Cell(CONFIGURATIONS_BY_KEY["nanofaas-p1-jvm"], BUDGET, 1)
INSIDE = "201 java /nanolab.slice/nanolab-sut.slice/docker-ab.scope\n"


def _protocol():
    config = ScenarioConfig.model_validate(
        yaml.safe_load((SCENARIOS / "experiment-02-pilot.yaml").read_text())
    )
    assert config.experiment_02 is not None
    return config.experiment_02


def _sample(ts: float, oom: int) -> str:
    group = {
        "memoryCurrent": 100,
        "inactiveFile": 0,
        "cpuUsageUsec": int(ts * 1000),
        "nrThrottled": 0,
        "throttledUsec": 0,
        "oomKill": oom,
    }
    return json.dumps({"ts": ts, "cgroups": {".": group}})


@dataclass
class FakePlatform:
    """Records what the cell asks of it; readiness answers as configured."""

    ready: bool = True
    calls: list[str] = field(default_factory=list)

    metrics_url: str | None = None

    def start(self, vm: Vm, *, protocol, payload, evidence):
        self.calls.append("start")
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

    def record(self, vm: Vm, evidence: Path) -> None:
        self.calls.append("record")

    def teardown(self, vm: Vm) -> None:
        self.calls.append("teardown")


def _stack(
    *,
    budget: str = "200000 100000\n2147483648\n0\n",
    listing: str = INSIDE,
    leftovers: list[str] | None = None,
    oom: tuple[int, int] = (0, 0),
) -> RecordingProvider:
    remaining = list(leftovers or [])

    def handler(argv, _env):
        if argv[:3] == ("docker", "ps", "-a"):
            return Result(0, "\n".join(["nanofaas-e2e-registry", *remaining]) + "\n")
        if argv[:3] == ("docker", "rm", "-f"):
            remaining.clear()
            return None
        if argv[0] == "cat":
            return Result(0, budget)
        if argv[0] == "sh" and "/proc/" in argv[-1]:
            return Result(0, listing)
        if argv[:3] == ("sudo", "systemctl", "stop"):
            provider.files[SAMPLES_REMOTE] = (
                "\n".join(
                    _sample(t, oom[0] if t < 50 else oom[1]) for t in range(0, 100, 5)
                )
                + "\n"
            ).encode()
            return None
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


def test_cell_measures_in_order(tmp_path):
    stack, loadgen, platform = _stack(), _loadgen(), FakePlatform()
    assert _run(stack, loadgen, platform, tmp_path) == "measured"
    commands = stack.commands()
    order = [
        _index(commands, ("sudo", "systemctl", "set-property")),
        _index(commands, ("sudo", "systemctl", "restart")),
        _index(commands, ("cat",)),
        _index(commands, ("sudo", "systemd-run")),
    ]
    assert order == sorted(order)
    assert platform.calls == ["start", "record", "teardown"]
    assert (tmp_path / LOAD_STARTED).is_file()
    assert (tmp_path / SAMPLES).is_file()
    assert (tmp_path / "k6-events.jsonl").is_file()
    assert json.loads((tmp_path / "budget.json").read_text())["observed"] == {
        "cpus": 2.0,
        "memoryMiB": 2048,
    }
    sampler = commands[_index(commands, ("sudo", "systemd-run"))]
    assert "--slice" in sampler
    assert sampler[sampler.index("--slice") + 1] == "nanolab-infra.slice"
    assert SAMPLER_UNIT in sampler


def test_readiness_timeout_is_startup_failed_with_diagnostics(tmp_path):
    stack, loadgen, platform = _stack(), _loadgen(), FakePlatform(ready=False)
    assert _run(stack, loadgen, platform, tmp_path) == "startup_failed"
    assert any((tmp_path / DIAGNOSTICS).iterdir())
    assert not any(argv[:2] == ("k6", "run") for argv in loadgen.commands())
    assert platform.calls[-1] == "teardown"
    assert not (tmp_path / LOAD_STARTED).exists()


def test_oom_kill_during_load_is_oom(tmp_path):
    stack, loadgen, platform = _stack(oom=(0, 1)), _loadgen(), FakePlatform()
    assert _run(stack, loadgen, platform, tmp_path) == "oom"
    assert (tmp_path / DIAGNOSTICS / "oom.txt").read_text().startswith("1 ")


def test_budget_mismatch_stops_before_deploy(tmp_path):
    stack = _stack(budget="100000 100000\n2147483648\n0\n")
    platform = FakePlatform()
    with pytest.raises(ValueError, match="cpu1-mem2048"):
        _run(stack, _loadgen(), platform, tmp_path)
    assert platform.calls == []
    assert not any(argv[:2] == ("sudo", "systemd-run") for argv in stack.commands())


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


def test_teardown_runs_even_when_k6_fails(tmp_path):
    platform = FakePlatform()
    with pytest.raises(RuntimeError, match="k6"):
        _run(_stack(), _loadgen(code=107, summary=False), platform, tmp_path)
    assert platform.calls[-1] == "teardown"
    assert (tmp_path / SAMPLES).is_file()
