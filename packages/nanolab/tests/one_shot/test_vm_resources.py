"""Whole-VM telemetry: independent counters, identities and missing-data evidence."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nanolab.one_shot.vm_resources import (
    correlate_sample,
    summarize_resources,
    telemetry_policy,
)
from nanolab.one_shot.vm_sampler import (
    container_delta,
    container_identity,
    cpu_delta,
    memory_values,
)


def test_cpu_excludes_guest_double_count_and_distinguishes_iowait_steal():
    before = "cpu 100 0 20 500 10 0 0 0 30 0\ncpu0 0\ncpu1 0\n"
    after = "cpu 160 0 30 520 15 2 3 5 60 0\ncpu0 0\ncpu1 0\n"
    result = cpu_delta(before, after)
    assert result["vcpuCount"] == 2
    assert result["cpuUtilization"] == pytest.approx(75 / 105)
    assert result["cpuUsedCores"] == pytest.approx(150 / 105)
    assert result["cpuIowaitFraction"] == pytest.approx(5 / 105)
    assert result["cpuStealFraction"] == pytest.approx(5 / 105)


@pytest.mark.parametrize(
    "after",
    ["cpu 1 0 0 0 0 0 0 0\ncpu0 0\n", "cpu 100 0 20 500 10 0 0 0\ncpu0 0\ncpu1 0\n"],
)
def test_invalid_or_nonadvancing_cpu_counters_are_not_zero(after):
    with pytest.raises(ValueError, match=r"invalid|unavailable|reset|scope|three"):
        cpu_delta("cpu 100 0 20 500 10 0 0 0\ncpu0 0\ncpu1 0\n", after)


def test_memory_uses_available_and_bytes_not_free_or_container_memory():
    row = memory_values(
        "MemTotal: 2048 kB\nMemAvailable: 512 kB\nMemFree: 64 kB\n"
        "SwapTotal: 128 kB\nSwapFree: 32 kB\n"
    )
    assert row == {
        "memoryTotalBytes": 2097152,
        "memoryAvailableBytes": 524288,
        "memoryUsedBytes": 1572864,
        "swapTotalBytes": 131072,
        "swapFreeBytes": 32768,
        "swapUsedBytes": 98304,
    }
    with pytest.raises(ValueError, match=r"invalid|unavailable|reset|scope|three"):
        memory_values("MemTotal: 2048 kB\n")


def test_container_delta_preserves_core_quota_and_throttling_units():
    previous = {
        "cpuUsageUsec": 100,
        "nrPeriods": 10,
        "nrThrottled": 2,
        "throttledUsec": 20,
    }
    current = {
        "cpuUsageUsec": 250100,
        "nrPeriods": 20,
        "nrThrottled": 6,
        "throttledUsec": 200020,
    }
    row = container_delta(previous, current, elapsed=1, vcpus=2, quota=0.5)
    assert row["cpuUsedCores"] == 0.25
    assert row["cpuVmFraction"] == 0.125
    assert row["cpuQuotaFraction"] == 0.5
    assert row["throttledPeriodsDelta"] == 4
    assert row["throttledUsecDelta"] == 200000
    with pytest.raises(ValueError, match=r"invalid|unavailable|reset|scope|three"):
        container_delta(current, previous, elapsed=1, vcpus=2, quota=0.5)


def test_distinct_container_roles_and_replicas():
    assert (
        container_identity(
            {
                "Id": "cp",
                "Name": "/nanolab-one-shot-control-plane",
                "Config": {"Labels": {}},
            }
        )["role"]
        == "control-plane"
    )
    assert (
        container_identity(
            {"Id": "fb", "Name": "/nanolab-logs-edge1-abc", "Config": {"Labels": {}}}
        )["role"]
        == "collector"
    )
    row = container_identity(
        {
            "Id": "fn",
            "Name": "/fn",
            "Config": {
                "Labels": {
                    "io.nanofaas.managed": "true",
                    "io.nanofaas.function": "edge1/arithmetic-rust",
                    "io.nanofaas.replica": "3",
                }
            },
        }
    )
    assert (row["role"], row["function"], row["replica"]) == (
        "function",
        "arithmetic-rust",
        3,
    )


def test_warmup_and_all_nine_epochs_have_exact_boundary_correlation():
    anchor = datetime(2026, 10, 10, tzinfo=UTC)
    for offset, phase, epoch in [
        (-720, "warmup", -1),
        (-1, "warmup", -1),
        (0, "measurement", 0),
        (359, "measurement", 0),
        (360, "measurement", 1),
        (3239, "measurement", 8),
        (3240, "after", None),
    ]:
        row = correlate_sample(
            {"timestampUtc": (anchor + timedelta(seconds=offset)).isoformat()},
            anchor=anchor,
            warmup=720,
            period=360,
            epochs=9,
            mode="ewma",
            functions=["a", "b"],
            run_id="r",
        )
        assert (row["phase"], row["epoch"], row["mode"], row["functions"]) == (
            phase,
            epoch,
            "ewma",
            ["a", "b"],
        )


def test_missing_duplicate_and_failed_run_raw_are_preserved(tmp_path):
    anchor = datetime(2026, 10, 10, tzinfo=UTC)
    directory = tmp_path / "vm-resources"
    directory.mkdir()
    path = directory / "edge1.jsonl"
    samples = [
        {
            "timestampUtc": (anchor + timedelta(seconds=i)).isoformat(),
            "node": "edge1",
            "cpuUtilization": 0.8,
            "containers": [],
        }
        for i in [-2, -1, 0, 0, 2]
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in samples) + "{partial")
    original = path.read_bytes()
    result = summarize_resources(
        tmp_path,
        anchor=anchor,
        warmup=2,
        period=3,
        epochs=1,
        nodes=["edge1", "cloud"],
        mode="ewma",
        functions=["a"],
        run_id="r",
    )
    assert not result["complete"]
    assert result["nodes"]["edge1"]["missingSeconds"] == [1]
    assert result["nodes"]["edge1"]["duplicateSeconds"] == [0]
    assert result["nodes"]["cloud"]["missingSeconds"] == [-2, -1, 0, 1, 2]
    assert result["nodes"]["edge1"]["malformedLines"] == 1
    assert path.read_bytes() == original
    assert result["nodes"]["edge1"]["rawSha256"]


def test_frozen_policy_binds_the_actual_sampler_bytes():
    import hashlib

    from nanolab.one_shot import vm_sampler

    policy = telemetry_policy()
    assert policy["periodSeconds"] == 1
    assert (
        policy["samplerSha256"]
        == hashlib.sha256(Path(vm_sampler.__file__).read_bytes()).hexdigest()
    )


def test_failed_run_report_reads_vm_raw_without_completion_evidence(tmp_path):
    from nanolab.one_shot.report import render_report
    from tests.one_shot.test_report import manifest

    value = manifest().model_copy(
        update={
            "anchor": "2026-10-10T00:00:00+00:00",
            "parameters": {
                "vmTelemetry": telemetry_policy(),
                "generator": {"warmupSeconds": 2},
                "plannedWindows": [{}],
            },
        }
    )
    directory = tmp_path / "vm-resources"
    directory.mkdir()
    row = {
        "timestampUtc": "2026-10-10T00:00:00+00:00",
        "node": "edge-0",
        "cpuUtilization": 0.8,
        "memoryTotalBytes": 1000,
        "memoryAvailableBytes": 200,
        "memoryUsedBytes": 800,
        "swapUsedBytes": 0,
        "containers": [
            {
                "containerId": "abc",
                "role": "function",
                "function": "work",
                "replica": 1,
                "incarnation": "1:123",
                "cpuUsedCores": 0.2,
                "memoryCurrentBytes": 64,
                "throttledUsecDelta": 100,
            }
        ],
    }
    (directory / "edge-0.jsonl").write_text(json.dumps(row) + "\n")
    output = render_report(value, None, tmp_path / "report.html")
    assert "Whole VM CPU: edge-0" in output.read_text()
    assert "Whole VM memory and swap: edge-0" in output.read_text()
    assert "Container CPU: edge-0" in output.read_text()
    result = json.loads(output.with_suffix(".json").read_text())
    assert not result["vmResources"]["complete"]
    assert result["vmResources"]["nodes"]["cloud"]["missingSeconds"]


@pytest.mark.parametrize("missing_initial", [False, True])
def test_streams_start_before_load_and_failed_cleanup_reaps_all_transports(
    tmp_path, monkeypatch, missing_initial
):
    from types import SimpleNamespace

    import nanolab.one_shot.vm_resources as module
    from nanolab.one_shot.vm_resources import VmTelemetrySession

    processes = []

    class Process:
        def __init__(self, argv, stdout, stderr, stdin):
            self.returncode = None
            self.waited = False
            processes.append(self)
            from io import BytesIO

            self.stdout = BytesIO(
                (
                    json.dumps(
                        {
                            "node": argv[argv.index("--node") + 1],
                            "timestampUtc": "2026-10-10T00:00:00+00:00",
                            "cpuUtilization": 0.8,
                        }
                    )
                    + "\n"
                ).encode()
            )
            self.stderr = BytesIO()

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.waited = True
            self.returncode = 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(module.subprocess, "Popen", Process)

    class Node:
        def __init__(self, name):
            self.config = SimpleNamespace(id=name)
            self.request = SimpleNamespace(name="owned-" + name)
            self.vm = object()
            self.provider = SimpleNamespace(
                transfer_to=lambda *a, **k: SimpleNamespace(return_code=0)
            )

        def command(self, argv):
            if argv[0] == "sha256sum":
                return SimpleNamespace(
                    stdout=telemetry_policy()["samplerSha256"] + " file"
                )
            if self.config.id == "edge1":
                raise RuntimeError("transport injected")
            return SimpleNamespace(stdout="")

    nodes = {name: Node(name) for name in ["edge1", "cloud"]}
    value = {
        "anchor": "2026-10-10T00:00:00+00:00",
        "period_seconds": 3,
        "run_id": "r",
        "parameters": {"generator": {"warmupSeconds": 2}},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(value))
    run = SimpleNamespace(
        run_dir=tmp_path,
        mode="ewma",
        settings=SimpleNamespace(functions={"a": {}}),
        trace=SimpleNamespace(windows=[{}], period_seconds=3),
        topology=SimpleNamespace(resources=SimpleNamespace(nodes=nodes)),
    )
    original = module.read_samples
    if missing_initial:
        run.supervision = object()
        monotonic = [0.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: monotonic[0])
        monkeypatch.setattr(
            module.time,
            "sleep",
            lambda seconds: monotonic.__setitem__(0, monotonic[0] + seconds),
        )
        monkeypatch.setattr(module, "read_samples", lambda _: ([], 0))
    session = VmTelemetrySession(
        run, SimpleNamespace(resource=lambda _: SimpleNamespace(home="/home/ubuntu"))
    )
    if missing_initial:
        monkeypatch.setattr(module, "read_samples", original)
        assert json.loads((tmp_path / "vm-resources/startup.json").read_text())[
            "missingNodes"
        ] == ["edge1", "cloud"]
    with pytest.raises(RuntimeError, match="cleanup failures"):
        session.close()
    assert all(p.waited for p in processes)
    assert (tmp_path / "vm-resources/edge1.jsonl").read_text()
    assert json.loads((tmp_path / "vm-resources/coverage.json").read_text())[
        "cleanupErrors"
    ]


def test_overhead_requires_three_complete_pairs_and_checks_every_node_function():
    from nanolab.one_shot.vm_resources import qualify_overhead

    cells = {
        "edge1/a": {"goodput": 10.0, "p95Seconds": 0.2},
        "cloud/a": {"goodput": 20.0, "p95Seconds": 0.1},
    }
    pairs = [{"off": cells, "on": cells} for _ in range(3)]
    assert qualify_overhead(pairs, expected_cells=set(cells))["qualified"]
    bad = [
        {"off": cells, "on": {**cells, "edge1/a": {"goodput": 8.0, "p95Seconds": 0.3}}},
        *pairs[1:],
    ]
    assert not qualify_overhead(bad, expected_cells=set(cells))["qualified"]
    with pytest.raises(ValueError, match=r"invalid|unavailable|reset|scope|three"):
        qualify_overhead(pairs[:2], expected_cells=set(cells))
    with pytest.raises(ValueError, match=r"invalid|unavailable|reset|scope|three"):
        qualify_overhead(pairs, expected_cells={"missing/a"})


def test_scientific_resource_gate_rejects_missing_or_changed_collector(tmp_path):
    from types import SimpleNamespace

    from nanolab.config.one_shot import ArtifactReference
    from nanolab.one_shot.vm_resources import require_resource_overhead

    settings = SimpleNamespace(
        strict_memory=True,
        purpose="scientific-experiment",
        functions={"a": {}, "b": {}},
        observability=SimpleNamespace(vm_resource_overhead=None),
    )
    with pytest.raises(ValueError, match="overhead"):
        require_resource_overhead(settings)
    path = tmp_path / "gate.json"
    data = {
        "qualified": True,
        "policy": {**telemetry_policy(), "samplerSha256": "0" * 64},
        "nanolabPackageSha256": "0" * 64,
    }
    path.write_text(json.dumps(data))
    import hashlib

    settings.observability.vm_resource_overhead = ArtifactReference(
        path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    with pytest.raises(ValueError, match=r"scope|changed"):
        require_resource_overhead(settings)


def test_capacity_collection_is_opted_into_by_qualified_resource_gate():
    from nanolab.config.one_shot import ArtifactReference
    from nanolab.one_shot.vm_resources import capacity_telemetry_enabled
    from tests.one_shot.test_multi_function_trace import inputs

    settings, _ = inputs()
    assert not capacity_telemetry_enabled(settings)
    settings = settings.model_copy(
        update={
            "observability": settings.observability.model_copy(
                update={
                    "vm_resource_overhead": ArtifactReference(
                        path=Path("/tmp/gate.json"), sha256="a" * 64
                    )
                }
            )
        }
    )
    assert capacity_telemetry_enabled(settings)


@pytest.mark.parametrize("logical", [False, True])
def test_timing_load_keeps_same_sampler_active_and_preserves_auction_error(
    tmp_path, monkeypatch, logical
):
    from types import SimpleNamespace

    from nanolab.tasks.one_shot.qualification_load import QualificationLoad

    actions = []
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.qualification_load.capacity_telemetry_enabled",
        lambda _: True,
    )

    class Sampler:
        def __init__(self, *args, terminal_probe=None):
            actions.append("sampler-start")
            self.terminal_probe = terminal_probe

        def require_healthy(self):
            actions.append("sampler-health")

        def close(self, **kwargs):
            actions.append("sampler-close")
            if self.terminal_probe is not None:
                self.terminal_probe()

    monkeypatch.setattr("nanolab.one_shot.vm_resources.VmTelemetrySession", Sampler)
    child = SimpleNamespace(poll=lambda: None)
    resource = SimpleNamespace(
        acquire=lambda _: actions.append("generator-start") or child,
        release=lambda *_: actions.append("generator-stop"),
    )
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.generator_resource", lambda **_: resource
    )

    class Collector:
        def __init__(self, *args):
            pass

        def require_healthy(self):
            pass

        def close(self):
            actions.append("proof-close")

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.LiveEvidenceCollector", Collector
    )
    monkeypatch.setattr(QualificationLoad, "audit", lambda *a, **k: {"valid": True})
    topology = SimpleNamespace(
        function_aliases={"a": "a"},
        function_settings={"a": SimpleNamespace(input={})},
        endpoints={"edge1": "url"},
    )
    settings = SimpleNamespace(
        timing=SimpleNamespace(minimum_samples=1),
        protocol_version=2 if logical else 1,
        flow_quantum=1,
        seed=7,
        purpose="workflow-validation",
    )
    load = QualificationLoad(
        topology,
        settings,
        SimpleNamespace(resource=lambda _: "http://edge1"),
        cell=SimpleNamespace(rates={"edge1": {"a": 1}}, id="cell"),
        period=360,
        run_dir=tmp_path / "timing",
        clock=SimpleNamespace(
            require_healthy=lambda: None,
            refresh=lambda: actions.append("terminal-clock"),
        ),
    )
    with pytest.raises(RuntimeError, match="original-auction"), load:
        raise RuntimeError("original-auction")
    assert actions.index("sampler-start") < actions.index("generator-start")
    assert actions.index("generator-stop") < actions.index("sampler-close")
    assert "sampler-health" in actions
    if logical:
        assert actions.index("sampler-close") < actions.index("terminal-clock")
        manifest = json.loads((tmp_path / "timing/manifest.json").read_text())
        assert manifest["parameters"]["logicalSchedule"]["protocolVersion"] == 2
        assert manifest["parameters"]["logicalSchedule"]["probeScope"] == "parent"
    else:
        assert "terminal-clock" not in actions


def test_protocol_rejects_changed_vm_sampling_period(tmp_path, monkeypatch):
    from nanolab.one_shot.models import CampaignProtocol
    from tests.one_shot.test_synchronized_campaign import protocol_fixture

    protocol, _, _ = protocol_fixture(tmp_path, monkeypatch)
    protocol["analysis"]["vmTelemetry"]["periodSeconds"] = 5
    with pytest.raises(ValueError, match="telemetry"):
        CampaignProtocol.model_validate(protocol)


def test_missing_gate_fails_before_any_vm_resource_is_composed(tmp_path, monkeypatch):
    from sonata_engine import Workflow

    from nanolab.config import EnvironmentConfig, ScenarioConfig
    from nanolab.plans.one_shot_common import add_one_shot_platforms
    from tests.one_shot.test_multi_function_trace import inputs

    settings, _ = inputs()
    config = ScenarioConfig.model_validate(
        {
            "workflow": "one-shot-calibration",
            "backend": "container",
            "functions": list(settings.functions),
            "oneShot": settings.model_dump(by_alias=True),
        }
    )
    calls = []
    monkeypatch.setattr(
        "nanolab.plans.one_shot_common.build_one_shot_resources",
        lambda *a, **k: calls.append("unexpected-vm"),
    )
    with pytest.raises(ValueError, match="overhead"):
        add_one_shot_platforms(
            Workflow("missing-gate"),
            config,
            EnvironmentConfig.model_validate(
                {"provider": "multipass", "roles": {"stack": {"name": "one-shot"}}}
            ),
            run_dir=tmp_path / "run",
            repo_root=tmp_path,
        )
    assert not calls and not (tmp_path / "run").exists()


def test_cgroup_read_preserves_memory_swap_and_rejects_pid_incarnation_change(
    tmp_path, monkeypatch
):
    from nanolab.one_shot import vm_sampler

    real_path = Path
    monkeypatch.setattr(
        vm_sampler, "Path", lambda value: real_path(tmp_path) / str(value).lstrip("/")
    )
    directory = tmp_path / "sys/fs/cgroup/owned"
    directory.mkdir(parents=True)
    proc = tmp_path / "proc/99"
    proc.mkdir(parents=True)
    (proc / "cgroup").write_text("0::/owned\n")
    for name, text in {
        "cpu.stat": (
            "usage_usec 1000000\nnr_periods 12\nnr_throttled 6\n"
            "throttled_usec 1500000\n"
        ),
        "cpu.max": "10000 100000",
        "memory.current": "12000000",
        "memory.max": "67108864",
        "memory.peak": "24000000",
        "memory.swap.current": "0",
        "memory.swap.max": "0",
        "memory.events": "oom 0\noom_kill 0\n",
    }.items():
        (directory / name).write_text(text)
    identities = iter(["99:123", "99:123"])
    monkeypatch.setattr(vm_sampler, "process_identity", lambda _: next(identities))
    inspect = {
        "Id": "abc",
        "Name": "/fn",
        "State": {"Pid": 99, "Running": True},
        "Config": {
            "Labels": {
                "io.nanofaas.managed": "true",
                "io.nanofaas.function": "edge1/a",
                "io.nanofaas.replica": "2",
            }
        },
    }
    row = vm_sampler.read_container(inspect)
    assert row["memoryLimitBytes"] == 67108864 and row["memoryCurrentBytes"] == 12000000
    assert row["swapLimitBytes"] == 0 and row["cpuQuotaCores"] == 0.1
    assert row["throttledUsec"] == 1500000
    identities = iter(["99:123", "99:124"])
    with pytest.raises(ValueError, match="incarnation"):
        vm_sampler.read_container(inspect)


def test_cached_docker_pid_cannot_be_reused_as_another_container(monkeypatch):
    from nanolab.one_shot import vm_sampler
    from nanolab.one_shot.vm_sampler import read_container

    # Fail before touching a cgroup when a recycled PID disagrees with the
    # process incarnation captured with the Docker inventory.
    monkeypatch.setattr(vm_sampler, "process_identity", lambda _: "99:124")
    inspect = {"State": {"Pid": 99, "Running": True}, "_samplerIncarnation": "99:123"}
    with pytest.raises(ValueError, match="cached Docker PID"):
        read_container(inspect)


def test_missing_container_counters_invalidate_resource_coverage(tmp_path):
    anchor = datetime(2026, 10, 10, tzinfo=UTC)
    directory = tmp_path / "vm-resources"
    directory.mkdir()
    row = {
        "timestampUtc": anchor.isoformat(),
        "node": "edge1",
        "cpuUtilization": 0.5,
        "vcpuCount": 1,
        "memoryTotalBytes": 1000,
        "memoryAvailableBytes": 800,
        "memoryUsedBytes": 200,
        "swapTotalBytes": 0,
        "swapFreeBytes": 0,
        "swapUsedBytes": 0,
        "containers": [],
        "expectedContainerIds": ["cp"],
        "errors": [],
    }
    (directory / "edge1.jsonl").write_text(json.dumps(row) + "\n")
    result = summarize_resources(
        tmp_path,
        anchor=anchor,
        warmup=0,
        period=1,
        epochs=1,
        nodes=["edge1"],
        mode="ewma",
        functions=["a"],
        run_id="r",
    )
    assert not result["complete"]
    assert result["nodes"]["edge1"]["missingContainerSamples"] == [
        {"second": 0, "containerIds": ["cp"]}
    ]


def test_pipe_only_transport_streams_before_exit_and_retains_terminal_bytes(
    tmp_path, monkeypatch
):
    import subprocess
    import sys
    from types import SimpleNamespace

    import nanolab.one_shot.vm_resources as module

    # Multipass rejects nonpollable output descriptors. Use real child pipes,
    # including a stderr payload larger than pipe capacity before the first row.
    child = tmp_path / "pipe_only_transport.py"
    child.write_text(
        "import json, os, signal, stat, sys, time\n"
        "if not all(stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (1, 2)):\n"
        "    sys.exit(7)\n"
        "def stop(*args):\n"
        "    print(json.dumps({'node':'edge1','terminal':True}), flush=True)\n"
        "    sys.stderr.write('terminal-partial'); sys.stderr.flush()\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "sys.stderr.write('x' * 131072); sys.stderr.flush()\n"
        "print(json.dumps({'node':'edge1','timestampUtc':"
        "'2026-10-10T00:00:00+00:00','cpuUtilization':0.8}), flush=True)\n"
        "while True: time.sleep(.1)\n"
    )
    processes = []
    popen = subprocess.Popen

    def start(_argv, **kwargs):
        process = popen((sys.executable, "-u", str(child)), **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", start)

    class Node:
        config = SimpleNamespace(id="edge1")
        request = SimpleNamespace(name="owned-edge1")
        vm = object()
        provider = SimpleNamespace(
            transfer_to=lambda *args, **kwargs: SimpleNamespace(return_code=0)
        )

        def command(self, argv):
            if argv[0] == "sha256sum":
                return SimpleNamespace(stdout=telemetry_policy()["samplerSha256"])
            processes[0].terminate()
            return SimpleNamespace(stdout="")

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "anchor": "2026-10-10T00:00:00+00:00",
                "period_seconds": 3,
                "run_id": "transport",
                "parameters": {"generator": {"warmupSeconds": 2}},
            }
        )
    )
    run = SimpleNamespace(
        run_dir=tmp_path,
        mode="ewma",
        settings=SimpleNamespace(functions={"a": {}}),
        trace=SimpleNamespace(windows=[{}], period_seconds=3),
        topology=SimpleNamespace(resources=SimpleNamespace(nodes={"edge1": Node()})),
    )
    session = module.VmTelemetrySession(
        run, SimpleNamespace(resource=lambda _: SimpleNamespace(home="/home/ubuntu"))
    )
    assert processes[0].poll() is None
    directory = tmp_path / "vm-resources"
    assert json.loads((directory / "edge1.jsonl").read_text())["node"] == "edge1"
    session.close()
    assert processes[0].returncode == 0
    rows = [
        json.loads(row) for row in (directory / "edge1.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["terminal"] is True
    assert (
        directory / "edge1.stderr"
    ).read_bytes() == b"x" * 131072 + b"terminal-partial"


@pytest.mark.parametrize(
    ("off_goodput", "on_goodput", "off_p95", "on_p95"),
    [
        (20.0, 19.0, 1.0, 1.0),
        (20 / 60, 19 / 60, 1.0, 1.0),
        (1940 / 60, 1843 / 60, 1.0, 1.0),
        (20.0, 20.0, 1.0, 1.05),
        (20.0, 19.0, 1.0, 1.05),
    ],
)
def test_exact_five_percent_is_inclusive(off_goodput, on_goodput, off_p95, on_p95):
    from nanolab.one_shot.vm_resources import qualify_overhead

    pair = {
        "off": {"cell": {"goodput": off_goodput, "p95Seconds": off_p95}},
        "on": {"cell": {"goodput": on_goodput, "p95Seconds": on_p95}},
    }
    assert qualify_overhead([pair] * 3, expected_cells={"cell"})["qualified"]


@pytest.mark.parametrize(
    ("off_goodput", "on_goodput", "off_p95", "on_p95"),
    [
        (20.0, 19.0 - 1e-8, 1.0, 1.0),
        (20.0, 20.0, 1.0, 1.05 + 1e-8),
        (35.0, 33.0, 1.0, 1.0),
        (20.0, 20.0, 1.0, 1.0606147477432402),
    ],
)
def test_excess_cost_is_rejected(off_goodput, on_goodput, off_p95, on_p95):
    from nanolab.one_shot.vm_resources import qualify_overhead

    pair = {
        "off": {"cell": {"goodput": off_goodput, "p95Seconds": off_p95}},
        "on": {"cell": {"goodput": on_goodput, "p95Seconds": on_p95}},
    }
    assert not qualify_overhead([pair] * 3, expected_cells={"cell"})["qualified"]


def test_sampler_preserves_inventory_when_all_cgroup_reads_fail(monkeypatch, capsys):
    from nanolab.one_shot import vm_sampler

    class Socket:
        def get_json(self, path):
            if path == "/containers/json":
                return [
                    {
                        "Id": "cp",
                        "Names": ["/nanolab-one-shot-control-plane"],
                        "Labels": {},
                    },
                    {"Id": "log", "Names": ["/nanolab-logs-edge"], "Labels": {}},
                ]
            raise OSError("container counters unavailable")

        def close(self):
            pass

    monkeypatch.setattr(vm_sampler, "DockerSocket", Socket)
    monkeypatch.setattr(vm_sampler.signal, "signal", lambda *_: None)
    vm_sampler.sample_loop("edge", duration=0.01)
    row = json.loads(capsys.readouterr().out.splitlines()[0])
    assert {item["role"] for item in row["containerInventory"]} == {
        "control-plane",
        "collector",
    }
    assert row["containers"] == []
    assert len(row["errors"]) == 2
