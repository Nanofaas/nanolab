"""Keep the initial experiment explicitly unqualified and bound to saved inputs."""

import importlib.util
import json
import signal
from pathlib import Path
from typing import Any, cast

import pytest
from sonata_engine import Workflow

from nanolab.plans import one_shot_observation as observation_plan
from nanolab.tasks.one_shot import observation as observation_tasks


@pytest.fixture
def observer():
    path = Path(__file__).parents[4] / "scripts/one-shot-observe.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("first_observation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_binding_keeps_resources_and_load_but_uses_one_observed_oracle_run(
    observer, tmp_path
):
    assert observer is not None, "first-observation script is missing"
    source = {
        "workflow": "one-shot-calibration",
        "oneShot": {
            "nodes": [{"id": "a", "cpus": 1}],
            "functions": {"f": {"cpu": 0.1, "memoryMiB": 128}},
            "timing": {
                "matrix": [
                    {"id": "one", "network": "local", "rates": {"a": {"f": 2.5}}}
                ]
            },
            "observability": {"logs": {"enabled": True}},
        },
    }
    distribution = tmp_path / "distribution.json"
    distribution.write_text(
        json.dumps({"source": {"revision": "a" * 40, "dirty": False}})
    )
    result = observer.bind_scenario(source, distribution)
    assert result["oneShot"]["nodes"] == source["oneShot"]["nodes"]
    assert result["oneShot"]["functions"] == source["oneShot"]["functions"]
    assert result["oneShot"]["experiment"]["windows"][0]["rates"] == {"a": {"f": 2.5}}
    assert result["oneShot"]["experiment"]["warmupSeconds"] == 720
    assert result["oneShot"]["experiment"]["repetitions"] == 1
    assert result["oneShot"]["experiment"]["maxArrivalLatenessSeconds"] == 3600
    assert result["oneShot"]["timing"]["protocol"]["preparationSeconds"] == 10
    assert result["oneShot"]["runtimeDistribution"]["sha256"] == observer.sha(
        distribution
    )
    assert "experiment" not in source["oneShot"]
    assert "protocol" not in source["oneShot"]["timing"]


def test_adapted_native_profile_preserves_real_historical_statistics(observer):
    assert observer is not None, "first-observation script is missing"
    parameters = {
        "f": {"cpu": 0.1, "memoryMiB": 128, "maxReplicas": 6, "input": {"x": 7}}
    }
    statistics = {
        "f": {
            "sampleCount": 123,
            "meanSeconds": 1.2,
            "p95Seconds": 1.5,
            "stddevSeconds": 0.2,
        }
    }
    profile = observation_tasks.native_profile(
        "a",
        parameters,
        statistics,
        {"f": "sha256:" + "b" * 64},
        revision="a" * 40,
        fingerprint="sha256:" + "c" * 64,
        physical_sha="d" * 64,
        replicas=2,
    )
    assert profile.synthetic is True
    assert profile.purpose == "workflow-validation"
    row = profile.functions[0]
    assert row["serviceSeconds"] == 1.2
    assert row["statistics"] == statistics["f"]
    assert row["memoryMiB"] == 128 and row["replicas"] == 2
    assert cast(dict, row["measurement"])["rawSamplesHash"] == "sha256:" + "d" * 64


def test_frozen_file_hash_changes_stop_before_use(observer, tmp_path):
    assert observer is not None, "first-observation script is missing"
    file = tmp_path / "input.json"
    file.write_text("{}")
    frozen = {"files": {"input.json": observer.sha(file)}}
    observer.verify_files(tmp_path, frozen)
    file.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="frozen bytes changed"):
        observer.verify_files(tmp_path, frozen)


@pytest.mark.parametrize("field", ["maxOperationalFraction", "maxAuctionFraction"])
@pytest.mark.parametrize(
    "fraction", [1.01, 1.000001, 0, -1, float("nan"), float("inf"), True, None]
)
def test_invalid_native_fraction_is_rejected_offline(observer, field, fraction):
    protocol = {
        "maxOperationalFraction": 0.2,
        "maxAuctionFraction": 0.2,
        "periodSeconds": 360,
        "leadSeconds": 8,
        "protocol": {"auctionSeconds": 4, "preparationSeconds": 1},
    }
    protocol[field] = fraction
    with pytest.raises(ValueError, match=field):
        observer.validate_operator_protocol(protocol)


def test_invalid_fraction_stops_before_vm_inventory(observer, tmp_path, monkeypatch):
    from types import SimpleNamespace

    freeze = tmp_path / "freeze"
    freeze.mkdir()
    (freeze / "freeze.json").write_text(
        json.dumps(
            {
                "files": {},
                "hostBootId": Path("/proc/sys/kernel/random/boot_id")
                .read_text()
                .strip(),
                "nanolabPackageSha256": observer.package_fingerprint(),
                "python": observer.sys.version,
                "dependencies": {},
            }
        )
    )
    (freeze / "operator-protocol.json").write_text(
        json.dumps(
            {
                "maxOperationalFraction": 1.01,
                "maxAuctionFraction": 1.01,
            }
        )
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid configuration reached the VM lifecycle")

    monkeypatch.setattr(observer.subprocess, "run", forbidden)
    monkeypatch.setattr(signal, "signal", forbidden)
    output = tmp_path / "run"
    with pytest.raises(ValueError, match="maxOperationalFraction"):
        observer.run_observation(
            SimpleNamespace(freeze=freeze, output=output, archive=None)
        )
    assert not output.exists()


def test_native_timing_budget_is_validated_without_performance_gates(observer):
    protocol = {
        "maxOperationalFraction": 0.2,
        "maxAuctionFraction": 0.2,
        "periodSeconds": 360,
        "leadSeconds": 8,
        "protocol": {"auctionSeconds": 4, "preparationSeconds": 1},
    }
    observer.validate_operator_protocol(protocol)
    protocol["leadSeconds"] = 4
    with pytest.raises(ValueError, match="operational budget"):
        observer.validate_operator_protocol(protocol)


def test_observer_fraction_boundary_matches_actual_native_records(observer, tmp_path):
    import os
    import shutil
    import subprocess

    source = os.environ.get("ONE_SHOT_RUNTIME_SOURCE")
    if not source or not shutil.which("javac") or not shutil.which("java"):
        pytest.skip("requires the actual runtime source and JDK")
    package = (
        Path(source)
        / "platform/modules/offload/src/main/java"
        / "it/unimib/datai/nanofaas/modules/offload/oneshot"
    )
    harness = tmp_path / "TimingContract.java"
    harness.write_text("""
import java.net.URI;
import java.time.*;
import java.util.Map;
import it.unimib.datai.nanofaas.modules.offload.oneshot.api.OneShotSettings;
import it.unimib.datai.nanofaas.modules.offload.oneshot.coordination.EpochSettings;
public class TimingContract {
    public static void main(String[] args) {
        try {
            var negotiation = new EpochSettings(Duration.ofSeconds(4),
                Duration.ofMillis(150), Duration.ofMillis(150), 8, 4, 64, 16,
                Double.parseDouble(args[0]));
            String digest = "sha256:" + "a".repeat(64);
            new OneShotSettings(2, "profile", digest, "workflow-validation", true,
                URI.create("http://cloud:8080"), 832, .05, Duration.ofSeconds(360),
                Duration.ofSeconds(14), false, Instant.parse("2026-10-12T00:00:00Z"),
                Duration.ofSeconds(10), Double.parseDouble(args[1]), 1,
                Map.of("f", new OneShotSettings.Function(
                    1, digest, digest, .9, 1, 0, 0)),
                negotiation, 2000000, 67108864, "session");
            System.out.println("accepted");
        } catch (IllegalArgumentException failure) {
            System.out.println("rejected");
        }
    }
}
""")
    compiled = subprocess.run(
        [
            "javac",
            "-d",
            str(tmp_path),
            str(package / "coordination/EpochSettings.java"),
            str(package / "api/OneShotSettings.java"),
            str(harness),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    limit = observer.NATIVE_MAX_TIMING_FRACTION
    for auction, operational, expected in [
        (limit, limit, "accepted"),
        (1.000001, limit, "rejected"),
        (limit, 1.000001, "rejected"),
        (0.99, limit, "accepted"),
        (limit, 0.99, "accepted"),
    ]:
        result = subprocess.run(
            [
                "java",
                "-cp",
                str(tmp_path),
                "TimingContract",
                str(auction),
                str(operational),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


@pytest.mark.parametrize("ready", [False, True])
def test_bootstrap_waits_for_cloud_init_before_installing_packages(
    tmp_path, monkeypatch, ready
):
    from types import SimpleNamespace

    from nanolab.plans.one_shot_common import _bootstrap
    from nanolab.tasks.vm.models import VmRequest

    calls = []
    node = SimpleNamespace(
        vm="vm",
        request=VmRequest(lifecycle="multipass", name="owned"),
        provider=object(),
        config=SimpleNamespace(id="edge-1"),
    )
    vm = SimpleNamespace(
        name="owned", host="127.0.0.1", user="ubuntu", home="/home/ubuntu"
    )

    def command(argv, **kwargs):
        assert argv[:3] == ("multipass", "exec", "owned")
        calls.append("cloud-init")
        return SimpleNamespace(
            returncode=0 if ready else 1,
            stdout=json.dumps({"status": "done" if ready else "error"}),
            stderr="",
        )

    monkeypatch.setattr("nanolab.plans.one_shot_common.subprocess.run", command)
    monkeypatch.setattr(
        "nanolab.plans.one_shot_common.scenario_context", lambda *_: object()
    )
    monkeypatch.setattr(
        "nanolab.plans.one_shot_common.plan_vm_provision_base", lambda _: ()
    )
    monkeypatch.setattr(
        "nanolab.plans.one_shot_common.retarget_cloud_operations", lambda *_: ()
    )
    monkeypatch.setattr(
        "nanolab.plans.one_shot_common.run_bootstrap_operations",
        lambda *_args, **_kwargs: calls.append("install"),
    )
    resource = _bootstrap(cast(Any, node), tmp_path)
    inputs = SimpleNamespace(resource=lambda _: vm)
    if ready:
        resource.acquire(cast(Any, inputs))
        assert calls == ["cloud-init", "install"]
    else:
        with pytest.raises(RuntimeError, match="cloud-init"):
            resource.acquire(cast(Any, inputs))
        assert calls == ["cloud-init"]


def test_observation_stages_have_names_for_sonata_journaling(observer):
    assert observer is not None
    from sonata_engine import Steps, Workflow

    stages = [
        kind(object())
        for kind in [
            observation_tasks.WarmInstances,
            observation_tasks.ObserverPrecheck,
            observation_tasks.ObservationFreeze,
            observation_tasks.ConfigureOracle,
            observation_tasks.ObservationReport,
        ]
    ]
    workflow = Workflow("first-observation-stages")
    workflow.add(Steps(title="Initial observation", steps=tuple(stages)))
    workflow.compile()
    assert all(stage.title for stage in stages)


def test_observer_precheck_correlates_samples_with_parent_clock_probes(
    observer, tmp_path, monkeypatch
):
    from datetime import datetime
    from types import SimpleNamespace

    from nanolab.one_shot.vm_resources import summarize_resources

    assert observer is not None
    probes = [
        {
            "node": "edge-1",
            "remoteBootId": "guest-boot",
            "remoteMonotonicSeconds": 1000 + second / 2,
            "beforeMonotonicSeconds": 100 + second / 2,
            "afterMonotonicSeconds": 100 + second / 2 + 0.001,
            "errorType": None,
        }
        for second in range(181)
    ]
    (tmp_path / "clock-probes.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in probes)
    )
    coverage = {}

    class Sampler:
        def __init__(self, context, _inputs, *, terminal_probe):
            self.context = context
            self.terminal_probe = terminal_probe
            manifest = json.loads((context.run_dir / "manifest.json").read_text())
            raw = context.run_dir / "vm-resources"
            raw.mkdir()
            rows = [
                {
                    "timestampUtc": manifest["anchor"],
                    "node": "edge-1",
                    "bootId": "guest-boot",
                    "monotonicSeconds": 1030.5 + second,
                    "cpuUtilization": 0.2,
                    "vcpuCount": 1,
                    "memoryTotalBytes": 1024,
                    "memoryAvailableBytes": 512,
                    "memoryUsedBytes": 512,
                    "swapTotalBytes": 0,
                    "swapFreeBytes": 0,
                    "swapUsedBytes": 0,
                    "containers": [],
                }
                for second in range(60)
            ]
            (raw / "edge-1.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )

        def close(self):
            self.terminal_probe()
            manifest = json.loads((self.context.run_dir / "manifest.json").read_text())
            coverage.update(
                summarize_resources(
                    self.context.run_dir,
                    anchor=datetime.fromisoformat(manifest["anchor"]),
                    warmup=0,
                    period=60,
                    epochs=1,
                    nodes=["edge-1"],
                    mode="oracle",
                    functions=["arithmetic"],
                    run_id="observer-precheck",
                )
            )

    ticks = iter([100, 191])
    monkeypatch.setattr(observation_tasks.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(observation_tasks, "VmTelemetrySession", Sampler)
    run = SimpleNamespace(
        run_dir=tmp_path,
        topology=SimpleNamespace(resources=SimpleNamespace(nodes={"edge-1": None})),
        settings=object(),
        clock=object(),
    )
    inputs = SimpleNamespace(resource=lambda _: SimpleNamespace(refresh=lambda: None))
    observation_tasks.ObserverPrecheck(run).run(cast(Any, inputs))
    assert coverage["complete"] is True
    node = coverage["nodes"]["edge-1"]
    assert node["observedSeconds"] == 60
    assert node["missingSeconds"] == []
    assert node["uncorrelatedSamples"] == 0
    assert node["phaseCorrelationComplete"] is True


@pytest.mark.parametrize("execute_load", [False, True])
def test_real_observation_preparation_and_workflow_compile_without_acquiring_vms(
    observer, tmp_path, monkeypatch, execute_load
):
    import os
    from types import SimpleNamespace

    tuning = os.environ.get("ONE_SHOT_TUNING_ROOT")
    distribution = os.environ.get("ONE_SHOT_DISTRIBUTION")
    reference = os.environ.get("ONE_SHOT_REFERENCE_ROOT")
    source = os.environ.get("ONE_SHOT_RUNTIME_SOURCE")
    if not (tuning and distribution and reference and source):
        pytest.skip("requires preserved observation/tuning/runtime artifacts")
    assert observer is not None
    args = SimpleNamespace(
        output=tmp_path / "freeze",
        tuning=Path(tuning),
        distribution=Path(distribution),
        observations=Path(reference).parent / "observed-demands.json",
        source=Path(source),
    )
    observer.prepare(args)
    frozen = json.loads((args.output / "freeze.json").read_text())
    assert frozen["qualified"] is False
    observer.verify_files(args.output, frozen)
    assert (args.output / "package/nanolab/assets/k6/one-shot.js").is_file()
    config = json.loads((args.output / "operator-protocol.json").read_text())
    assert (
        config["performanceAcceptanceGates"] is False
        and config["requireNoSwap"] is False
    )
    assert config["maxOperationalFraction"] == config["maxAuctionFraction"] == 1
    assert config["protocol"]["preparationSeconds"] == 10
    assert config["leadSeconds"] == 90
    observer.validate_operator_protocol(config)
    commands = []
    original_run = observer.subprocess.run

    def inventory(argv, **_kwargs):
        if argv[0] == "git":
            return original_run(argv, **_kwargs)
        commands.append(argv)
        assert argv == ["multipass", "list", "--format", "json"]
        return SimpleNamespace(stdout='{"list": []}', returncode=0)

    monkeypatch.setattr(observer.subprocess, "run", inventory)
    monkeypatch.setattr(signal, "signal", lambda *_: None)
    monkeypatch.setattr("nanolab.one_shot.infrastructure.external_vm_cpus", lambda: 0)

    engine_run = Workflow.run
    actions = []

    def compile_only(workflow):
        compiled = workflow.compile()
        if execute_load:
            from sonata_engine import Resource, Workflow

            from tests.one_shot.test_experiment_task import manifest

            unit = next(
                row
                for row in compiled.tasks
                if isinstance(row.task, observation_plan.RunOneShotLoadTask)
            )
            task = unit.task
            (task.freeze.run_dir / "manifest.json").write_text(
                manifest().model_dump_json()
            )
            task.freeze.settings = task.freeze.settings.model_copy(
                update={
                    "experiment": task.freeze.settings.experiment.model_copy(
                        update={"fail_load": True}
                    ),
                }
            )

            class Child:
                returncode = None

                def poll(self):
                    return self.returncode

                def terminate(self):
                    actions.append("generator-terminate")
                    self.returncode = -15

                def wait(self, **_kwargs):
                    return self.returncode

            def resource(title, value, requires=()):
                return Resource(
                    title=title,
                    acquire=lambda _: value,
                    release=lambda *_: actions.append(title + "-released"),
                    requires=requires,
                )

            sampler = resource(
                "sampler",
                SimpleNamespace(
                    close=lambda: actions.append("telemetry-close"),
                ),
            )
            generator = resource("generator", Child(), requires=(sampler,))
            collector = resource(
                "collector",
                SimpleNamespace(
                    close=lambda: actions.append("collector-close"),
                ),
                requires=(generator,),
            )
            substitutes = {
                task.telemetry: sampler,
                task.generator: generator,
                task.collector: collector,
                task.freeze.clock: resource("clock", object()),
            }
            required = tuple(
                substitutes.get(original) or resource(original.title, object())
                for original in unit.required_resources
            )
            task.telemetry, task.generator, task.collector = (
                sampler,
                generator,
                collector,
            )
            task.freeze.clock = substitutes[task.freeze.clock]
            monkeypatch.setattr(
                "nanolab.tasks.one_shot.experiment.collect_runtime_evidence",
                lambda *_: None,
            )
            monkeypatch.setattr(
                "nanolab.tasks.one_shot.experiment.collect_original_evidence",
                lambda *_: None,
            )
            simulated = Workflow("execute-real-load-scope-without-vms")
            simulated.add(task, requires=required)
            engine_run(simulated)
        raise RuntimeError("intentional-stop-before-acquire")

    monkeypatch.setattr(Workflow, "run", compile_only)
    run_args = SimpleNamespace(
        freeze=args.output, output=tmp_path / "run", archive=None
    )
    expected = (
        "injected load-task failure"
        if execute_load
        else "intentional-stop-before-acquire"
    )
    with pytest.raises(RuntimeError, match=expected):
        observer.run_observation(run_args)
    assert len(commands) == 2
    assert expected in (run_args.output / "failure.json").read_text()
    if execute_load:
        assert "telemetry-close" in actions
        assert "collector-close" in actions
        assert "generator-terminate" in actions
        assert "sampler-released" in actions


def test_oracle_lifetime_covers_warmup_and_all_nine_intervals(observer):
    from types import SimpleNamespace

    trace = SimpleNamespace(period_seconds=360, windows=[{}] * 9)
    assert observer.oracle_lifetime_seconds(trace, warmup_seconds=720) == 4230


def test_actual_native_oracle_store_reproduces_expiry_and_accepts_campaign_lifetime(
    observer, tmp_path
):
    import os
    import shutil
    import subprocess

    source = os.environ.get("ONE_SHOT_RUNTIME_SOURCE")
    if not source or not shutil.which("javac") or not shutil.which("java"):
        pytest.skip("requires actual native source and JDK")
    from types import SimpleNamespace

    seconds = observer.oracle_lifetime_seconds(
        SimpleNamespace(period_seconds=360, windows=[{}] * 9), warmup_seconds=720
    )
    harness = tmp_path / "OracleLifetime.java"
    harness.write_text("""
import java.time.*;
import java.util.*;
import it.unimib.datai.nanofaas.forecastingapi.*;
import it.unimib.datai.nanofaas.modules.forecasting.OracleForecastStore;
public class OracleLifetime {
    public static void main(String[] args) {
        Instant produced = Instant.parse("2026-10-12T20:58:33Z");
        Instant anchor = produced.plusSeconds(810);
        var rows = new ArrayList<OracleForecastStore.Entry>();
        for (int e=0;e<9;e++) rows.add(new OracleForecastStore.Entry("f",1,
            anchor.plusSeconds(e*360),anchor.plusSeconds((e+1)*360),e+1,"requests/s"));
        var trace = new OracleForecastStore.Trace(1,"edge",1,"oracle",produced,rows);
        for (int e=0;e<9;e++) {
            var query = new ForecastQuery("edge","f",1,
                anchor.plusSeconds(e*360),anchor.plusSeconds((e+1)*360));
            Clock clock = Clock.fixed(anchor.plusSeconds(e*360-8),ZoneOffset.UTC);
            var expired = new OracleForecastStore(clock,Duration.ofMinutes(5));
            expired.replace(0,trace);
            if (expired.forecast(query).status()!=ForecastSnapshot.Status.STALE)
                throw new AssertionError("failed to reproduce expiry");
            var configured = new OracleForecastStore(clock,
                Duration.ofSeconds(Long.parseLong(args[0])));
            configured.replace(0,trace);
            if (configured.forecast(query).status()!=ForecastSnapshot.Status.AVAILABLE)
                throw new AssertionError("epoch missing: "+e);
            if (configured.forecast(query).rate()!=e+1)
                throw new AssertionError("changed rate");
            var other = new ForecastQuery("edge","f",2,query.start(),query.end());
            if (configured.forecast(other).status()!=ForecastSnapshot.Status.MISSING)
                throw new AssertionError("generation leaked");
        }
    }
}
""")
    root = Path(source)
    api = (
        root
        / "platform/libs/forecasting-api/src/main/java"
        / "it/unimib/datai/nanofaas/forecastingapi"
    )
    files = [
        api / (name + ".java")
        for name in ["ForecastSource", "ForecastQuery", "ForecastSnapshot"]
    ]
    files.append(
        root
        / "platform/modules/forecasting/src/main/java"
        / "it/unimib/datai/nanofaas/modules/forecasting/OracleForecastStore.java"
    )
    subprocess.run(
        ["javac", "-d", str(tmp_path), *[str(path) for path in files], str(harness)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["java", "-cp", str(tmp_path), "OracleLifetime", str(seconds)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_failure_fallback_counts_rejects_and_renders_after_writers_stop(
    observer, tmp_path
):
    from sonata_engine import Task, Workflow

    from nanolab.one_shot.supervision import CampaignSupervision
    from tests.one_shot.test_returned_errors import manifest

    planned = [
        {
            "originalId": "a",
            "phase": "warmup",
            "origin": "edge-0",
            "function": "work",
            "epoch": -1,
        }
    ]
    (tmp_path / "manifest.json").write_text(manifest().model_dump_json())
    (tmp_path / "schedule.json").write_text(json.dumps(planned))
    (tmp_path / "generator.jsonl").write_text(
        "\n".join(
            json.dumps({"msg": json.dumps(dict(planned[0], event=event, **fields))})
            for event, fields in [
                ("emitted", {}),
                ("result", {"statusCode": 429, "responseBody": "busy"}),
            ]
        )
    )

    class Failure(Task):
        title = "Operational fault after warmup"

        def run(self, inputs):
            raise RuntimeError("operational fault")

    workflow = Workflow("failed-observation")
    workflow.add(Failure())

    def execute():
        try:
            workflow.run()
        finally:
            observer.retain_offline_report(tmp_path)

    with pytest.raises(RuntimeError, match="operational fault"):
        CampaignSupervision(tmp_path, cadence=0.01).run(execute)
    summary = json.loads((tmp_path / "responses.json").read_text())
    assert summary["phases"]["warmup"]["total"]["reject"] == 1
    assert summary["phases"]["warmup"]["total"]["returnedErrorsPercentOfEmitted"] == 100
    assert (tmp_path / "report.html").exists()
    assert (
        json.loads((tmp_path / "result.json").read_text())["cause"]
        == "operational fault"
    )
