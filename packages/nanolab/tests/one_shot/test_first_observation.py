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


@pytest.mark.parametrize("profiled", [False, True])
@pytest.mark.parametrize("execute_load", [False, True])
def test_real_observation_preparation_and_workflow_compile_without_acquiring_vms(
    observer, tmp_path, monkeypatch, execute_load, profiled
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
        observations=Path(
            os.environ.get(
                "ONE_SHOT_OBSERVATIONS",
                str(Path(reference).parent / "observed-demands.json"),
            )
        ),
        source=Path(source),
    )
    if profiled:
        profile_input = os.environ.get("ONE_SHOT_ARRIVAL_PROFILE")
        if not profile_input:
            pytest.skip("requires exported continuous-arrival artifact")
        args.arrival_profile = Path(profile_input)
    observer.prepare(args)
    frozen = json.loads((args.output / "freeze.json").read_text())
    if profiled:
        assert frozen["arrivalProfile"]["sha256"] == observer.sha(args.arrival_profile)
        assert (
            args.output / "arrival-profile.json"
        ).read_bytes() == args.arrival_profile.read_bytes()
        assert (args.output / "package/nanolab/tasks/one_shot/arrivals.py").is_file()
    else:
        assert "arrivalProfile" not in frozen

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


@pytest.mark.parametrize("invalid", ["semantic", "tampered"])
def test_invalid_arrival_profile_stops_before_vm_inventory(
    observer, tmp_path, monkeypatch, invalid
):
    from types import SimpleNamespace

    from tests.one_shot.test_arrivals import arrival_case_data

    # Reuse a small valid trace/profile fixture without provisioning anything.
    trace, profile = arrival_case_data()
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    observation_tasks.save(freeze / "trace.json", trace.model_dump())
    profile["traceSha256"] = observer.sha(freeze / "trace.json")
    if invalid == "semantic":
        profile["cells"][0]["offsetsSeconds"][0] = True
    observation_tasks.save(freeze / "arrival-profile.json", profile)
    digest = observer.sha(freeze / "arrival-profile.json")
    observation_tasks.save(
        freeze / "freeze.json",
        {
            "files": {"arrival-profile.json": digest},
            "arrivalProfile": {"sha256": digest},
        },
    )
    observation_tasks.save(
        freeze / "operator-protocol.json",
        {
            "maxOperationalFraction": 1,
            "maxAuctionFraction": 1,
            "periodSeconds": 360,
            "leadSeconds": 90,
            "protocol": {"auctionSeconds": 60, "preparationSeconds": 10},
        },
    )
    if invalid == "tampered":
        (freeze / "arrival-profile.json").write_text("{}")

    def forbidden(*a, **k):
        pytest.fail("invalid profile entered operational workflow")

    monkeypatch.setattr(observer.subprocess, "run", forbidden)
    monkeypatch.setattr(signal, "signal", forbidden)
    with pytest.raises(ValueError, match=r"arrival|frozen"):
        observer.run_observation(
            SimpleNamespace(freeze=freeze, output=tmp_path / "run", archive=None)
        )
    assert not (tmp_path / "run").exists()


def test_invalid_arrival_profile_stops_prepare_before_output(observer, tmp_path):
    from types import SimpleNamespace

    from tests.one_shot.test_arrivals import arrival_case_data

    trace, profile = arrival_case_data()
    tuning = tmp_path / "tuning"
    tuning.mkdir()
    observation_tasks.save(tuning / "trace.json", trace.model_dump())
    profile["traceSha256"] = observer.sha(tuning / "trace.json")
    profile["cells"].pop()
    path = tmp_path / "profile.json"
    observation_tasks.save(path, profile)
    with pytest.raises(ValueError, match="arrival"):
        observer.prepare(
            SimpleNamespace(
                tuning=tuning, arrival_profile=path, output=tmp_path / "output"
            )
        )
    assert not (tmp_path / "output").exists()


def test_minute_observation_records_timing_instead_of_rejecting_maxima(observer):
    protocol = {
        "maxOperationalFraction": 1,
        "maxAuctionFraction": 1,
        "periodSeconds": 60,
        "leadSeconds": 90,
        "protocol": {"auctionSeconds": 60, "preparationSeconds": 10},
    }
    with pytest.raises(ValueError, match="operational budget"):
        observer.validate_operator_protocol(protocol)
    protocol["timingPolicy"] = "observe"
    observer.validate_operator_protocol(protocol)
    assert protocol["protocol"] == {"auctionSeconds": 60, "preparationSeconds": 10}


def test_derived_scenario_changes_grid_but_keeps_resources_and_timeouts(
    observer, tmp_path
):
    from tests.one_shot.test_arrivals import minute_replay_case

    trace, _, _ = minute_replay_case()
    source = {
        "oneShot": {
            "nodes": [{"id": "a", "cpus": 2}],
            "functions": {"f": {"cpu": 0.25, "memoryMiB": 64}},
            "timing": {"matrix": [{"id": "old", "rates": {"a": {"f": 1}}}]},
        }
    }
    distribution = tmp_path / "distribution.json"
    distribution.write_text("{}")
    scenario = observer.bind_scenario(source, distribution, trace=trace)
    assert scenario["oneShot"]["nodes"] == source["oneShot"]["nodes"]
    assert scenario["oneShot"]["functions"] == source["oneShot"]["functions"]
    assert len(scenario["oneShot"]["experiment"]["windows"]) == 54
    assert scenario["oneShot"]["timing"]["periodCandidatesSeconds"] == [60]
    assert scenario["oneShot"]["timing"]["protocol"]["auctionSeconds"] == 60
    assert scenario["oneShot"]["timing"]["protocol"]["preparationSeconds"] == 10


def test_parent_archive_integrity_does_not_require_retired_environments(
    observer, tmp_path
):
    content = tmp_path / "trace.json"
    content.write_text("{}")
    parent = {
        "files": {"trace.json": observer.sha(content)},
        "source": "/retired/checkout",
        "sourceFiles": {"missing.java": "old"},
        "dependencyFiles": {"/retired/environment/site-packages/module.py": "old"},
    }
    (tmp_path / "freeze.json").write_text(json.dumps(parent))
    fingerprint = observer.sha(tmp_path / "freeze.json")
    observer.verify_parent_archive(tmp_path, fingerprint)
    content.write_text('{"changed":true}')
    with pytest.raises(ValueError, match=r"parent.*bytes"):
        observer.verify_parent_archive(tmp_path, fingerprint)


def offline_observation(observer, tmp_path, monkeypatch, *, primary=None):
    """Prepare a valid frozen launcher and real Sonata workflow with fake resources."""
    import os
    import shutil
    from types import SimpleNamespace

    from sonata_engine import Resource

    from tests.one_shot.test_report import manifest
    from tests.one_shot.test_supervision import Action

    freeze, raw = tmp_path / "freeze", tmp_path / "raw"
    freeze.mkdir()
    observer.save(
        freeze / "freeze.json",
        {
            "files": {},
            "hostBootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "nanolabPackageSha256": observer.package_fingerprint(),
            "python": observer.sys.version,
            "dependencies": {},
        },
    )
    observer.save(
        freeze / "operator-protocol.json",
        {
            "maxOperationalFraction": 1,
            "maxAuctionFraction": 1,
            "periodSeconds": 360,
            "leadSeconds": 90,
            "protocol": {"auctionSeconds": 60, "preparationSeconds": 10},
        },
    )
    preset = (
        Path(__file__).parents[2]
        / "src/nanolab/assets/presets/scenarios"
        / "one-shot-calibration.yaml"
    )
    shutil.copyfile(preset, freeze / "scenario.yaml")
    releases = []
    resource = Resource(
        title="Simulated observation VM",
        acquire=lambda _: "vm",
        release=lambda _, value: releases.append(value),
    )

    def action(inputs):
        assert inputs.resource(resource) == "vm"
        (raw / "manifest.json").write_text(manifest().model_dump_json())
        if isinstance(primary, int):
            os.kill(os.getpid(), primary)
        elif primary is not None:
            raise RuntimeError(primary)

    workflow = Workflow("offline-observation")
    workflow.add(Action(action), requires=(resource,))
    monkeypatch.setattr(
        observer, "build_one_shot_observation_plan", lambda *a, **k: workflow
    )
    return SimpleNamespace(freeze=freeze, output=raw, archive=None), releases


@pytest.mark.parametrize("inventory_fails", [False, True])
def test_final_inventory_failure_still_renders_offline_report(
    observer, tmp_path, monkeypatch, inventory_fails
):
    """Final inventory failure still renders offline report."""
    from types import SimpleNamespace

    args, releases = offline_observation(observer, tmp_path, monkeypatch)
    inventories = []

    def inventory(*a, **k):
        inventories.append(True)
        if len(inventories) == 2 and inventory_fails:
            raise OSError("final inventory failed")
        return SimpleNamespace(stdout='{"list":[]}')

    monkeypatch.setattr(observer.subprocess, "run", inventory)
    if inventory_fails:
        with pytest.raises(OSError, match="final inventory failed"):
            observer.run_observation(args)
    else:
        observer.run_observation(args)
    assert releases == ["vm"] and len(inventories) == 2
    assert (args.output / "inventory-failure.json").exists() is inventory_fails
    result = json.loads((args.output / "result.json").read_text())
    report = json.loads((args.output / "report.json").read_text())
    assert (args.output / "report.html").exists()
    assert result["state"] == ("failed" if inventory_fails else "completed")
    assert report["operationalOutcome"]["state"] == result["state"]
    assert result["cause"] == ("final inventory failed" if inventory_fails else None)


@pytest.mark.parametrize(
    "primary", [None, "workflow failed", signal.SIGINT, signal.SIGTERM]
)
@pytest.mark.parametrize("receipt_fails", [False, True])
def test_finalization_preserves_first_failure(
    observer, tmp_path, monkeypatch, primary, receipt_fails
):
    """Finalization preserves first failure."""
    from types import SimpleNamespace

    args, releases = offline_observation(
        observer, tmp_path, monkeypatch, primary=primary
    )
    inventories, reports = [], []
    save = observer.save
    render = observer.retain_offline_report

    def inventory(*a, **k):
        inventories.append(True)
        if len(inventories) == 2:
            raise OSError("final inventory failed")
        return SimpleNamespace(stdout='{"list":[]}')

    def save_receipt(path, data):
        if path.name == "inventory-failure.json" and receipt_fails:
            raise OSError("inventory receipt failed")
        save(path, data)

    def render_then_fail(directory):
        reports.append(True)
        render(directory)
        raise OSError("renderer failed")

    monkeypatch.setattr(observer.subprocess, "run", inventory)
    monkeypatch.setattr(observer, "save", save_receipt)
    monkeypatch.setattr(observer, "retain_offline_report", render_then_fail)
    cause = (
        "interrupted"
        if isinstance(primary, int)
        else primary or "final inventory failed"
    )
    with pytest.raises((RuntimeError, OSError), match=cause):
        observer.run_observation(args)
    result = json.loads((args.output / "result.json").read_text())
    assert cause in result["cause"]
    assert result["exitCode"] == (128 + primary if isinstance(primary, int) else 1)
    assert ("inventory receipt failed" in str(result["notes"])) is receipt_fails
    assert "renderer failed" in str(result["notes"])
    if primary is not None:
        assert "final inventory failed" in str(result["notes"])
    assert reports == [True] and releases == ["vm"]
    assert (
        json.loads((args.output / "report.json").read_text())["operationalOutcome"][
            "cause"
        ]
        == result["cause"]
    )


def test_renderer_failure_after_success_is_terminal_failure(
    observer, tmp_path, monkeypatch
):
    """Renderer failure after success is terminal failure."""
    from types import SimpleNamespace

    args, releases = offline_observation(observer, tmp_path, monkeypatch)
    monkeypatch.setattr(
        observer.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout='{"list":[]}'),
    )
    attempts = []

    def render(_):
        attempts.append(True)
        raise OSError("renderer failed")

    monkeypatch.setattr(observer, "retain_offline_report", render)
    with pytest.raises(OSError, match="renderer failed"):
        observer.run_observation(args)
    assert attempts == [True] and releases == ["vm"]
    result = json.loads((args.output / "result.json").read_text())
    assert result["cause"] == "renderer failed" and result["state"] == "failed"


@pytest.mark.parametrize("changed", [False, True])
def test_active_observation_entrypoint_must_match_frozen_script(
    observer, tmp_path, monkeypatch, changed
):
    """Reject executing different launcher bytes even when saved artifacts verify."""
    from types import SimpleNamespace

    args, releases = offline_observation(observer, tmp_path, monkeypatch)
    saved = args.freeze / "source-used.py"
    saved.write_bytes(Path(observer.__file__).read_bytes())
    frozen = json.loads((args.freeze / "freeze.json").read_text())
    frozen["files"]["source-used.py"] = observer.sha(saved)
    (args.freeze / "freeze.json").write_text(json.dumps(frozen))
    active = tmp_path / "active-observer.py"
    active.write_bytes(
        saved.read_bytes() + (b"\n# different launcher\n" if changed else b"")
    )
    monkeypatch.setattr(observer, "__file__", str(active))
    inventories = []

    def inventory(*a, **k):
        inventories.append(True)
        return SimpleNamespace(stdout='{"list":[]}')

    monkeypatch.setattr(observer.subprocess, "run", inventory)
    if changed:
        with pytest.raises(ValueError, match="observation entrypoint drift"):
            observer.run_observation(args)
        assert not inventories and not releases and not args.output.exists()
    else:
        observer.run_observation(args)
        assert len(inventories) == 2 and releases == ["vm"]


@pytest.mark.parametrize(
    "relative", ["one_shot/supervision.py", "cli/catalogue.py", "config/scenario.py"]
)
def test_changed_package_implementation_stops_before_observation_inventory(
    observer, tmp_path, monkeypatch, relative
):
    """The real package fingerprint includes each production fix before acquisition."""
    from nanolab.tasks.one_shot import artifacts

    package = tmp_path / "package"
    for name in ("one_shot/supervision.py", "cli/catalogue.py", "config/scenario.py"):
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# original implementation\n")
    monkeypatch.setattr(artifacts, "files", lambda _: package)
    args, releases = offline_observation(observer, tmp_path, monkeypatch)
    (package / relative).write_text("# fixed implementation\n")
    attempts = []

    def inventory(*a, **k):
        attempts.append(True)
        pytest.fail("package drift reached inventory")

    monkeypatch.setattr(observer.subprocess, "run", inventory)
    with pytest.raises(ValueError, match="host boot or package drift"):
        observer.run_observation(args)
    assert not attempts and not releases and not args.output.exists()
