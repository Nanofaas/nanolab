import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from nanolab.config.one_shot import OneShotConfig
from nanolab.one_shot.contracts import contract_asset
from nanolab.one_shot.models import CalibrationProfile, TimingQualification
from nanolab.tasks.one_shot.qualification import QualifyTimingTask
from nanolab.tasks.one_shot.timing import WallTimingSample


def task_fixture(tmp_path, monkeypatch, *, censored=False, release=True):
    preset = (
        Path(__file__).parents[2]
        / "src/nanolab/assets/presets/scenarios/one-shot-qualification.yaml"
    )
    data = yaml.safe_load(preset.read_text())["oneShot"]
    data["profile"] = {"path": str(tmp_path / "profile.json"), "sha256": "a" * 64}
    settings = OneShotConfig.model_validate(data)
    document = json.loads(
        contract_asset("examples/synthetic-profile.json").read_bytes()
    )
    document.update(provider="multipass", synthetic=False)
    profile = CalibrationProfile.model_validate(document)
    calls = []

    class Client:
        def __init__(self, url, **kwargs):
            self.base_url = url

        def load_profile(self, *args, **kwargs):
            calls.append((self.base_url, "profile"))

        def status(self):
            return SimpleNamespace(revision=0)

        def drain_and_release(self):
            calls.append((self.base_url, "release"))
            return release

    target = "nanolab.tasks.one_shot.qualification."
    monkeypatch.setattr(target + "NanoFaasOneShotClient", Client)
    monkeypatch.setattr(
        target + "verify_profile_scope", lambda *args: profile.environment_fingerprint
    )
    monkeypatch.setattr(target + "configuration_for_node", lambda *args, **kwargs: {})
    monkeypatch.setattr(target + "configure_node", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        target + "upload_window", lambda *args, **kwargs: kwargs["revision"] + 1
    )
    monkeypatch.setattr(target + "wait_until", lambda *args, **kwargs: None)

    def prepare(*args, **kwargs):
        calls.append((kwargs["epoch"], "prepare"))
        return WallTimingSample(
            None if censored else 0.1, None if censored else 0.2, censored
        ), {"censored": censored}

    monkeypatch.setattr(target + "prepare_parallel_epoch", prepare)
    monkeypatch.setattr(
        target + "capture_replica_diagnostics",
        lambda *args, **kwargs: calls.append((kwargs["epoch"], "diagnostics")),
    )

    class LoadedCell:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def require_healthy(self):
            pass

        def audit(self, **kwargs):
            return {"valid": True, "method": "owned-http-sdk-contention-v1"}

    monkeypatch.setattr(target + "QualificationLoad", LoadedCell, raising=False)
    topology = SimpleNamespace(
        resources=SimpleNamespace(
            nodes={node.id: SimpleNamespace(config=node) for node in settings.nodes}
        ),
        distribution="built",
        endpoints={node.id: node.id for node in settings.nodes},
        function_aliases={"one-shot-workload-rust": "one-shot-workload"},
        function_settings={
            "one-shot-workload": settings.functions["one-shot-workload-rust"]
        },
    )
    monitor = SimpleNamespace(require_healthy=lambda: None)
    inputs = SimpleNamespace(
        resource=lambda key: monitor if key == "clock" else key, upstream=lambda: None
    )
    task = QualifyTimingTask(
        topology, settings, profile, clock=cast(Any, "clock"), run_dir=tmp_path
    )  # pyright: ignore[reportArgumentType]
    return task, cast(Any, inputs), calls


def test_complete_matrix_publishes_immutable_qualification(tmp_path, monkeypatch):
    task, inputs, calls = task_fixture(tmp_path, monkeypatch)
    result = task.run(inputs).value
    assert result is not None and result.qualified
    assert result.sample_count == 20 and len(result.matrix) == 2
    assert result.period_seconds == 20 and result.lead_seconds == pytest.approx(1.9)
    assert [row[0] for row in calls if row[1] == "prepare"] == list(range(20))
    assert len([row for row in calls if row[1] == "release"]) == 2
    assert (
        TimingQualification.model_validate_json((tmp_path / "timing.json").read_bytes())
        == result
    )


@pytest.mark.parametrize("oom", [False, True])
def test_synchronized_matrix_has_450_samples_and_feasible_fixed_cloud(
    tmp_path, monkeypatch, oom
):
    from contextlib import nullcontext
    from copy import deepcopy

    import httpx

    from nanolab.config.one_shot import TimingSettings, resolve_node_functions
    from nanolab.one_shot.profiles import CalibrationProfiles
    from nanolab.tasks.one_shot.trace import (
        cloud_capacity_requirements,
        concurrent_trace,
    )
    from tests.one_shot.test_multi_function_trace import inputs as multi_inputs

    task, inputs, _calls = task_fixture(tmp_path, monkeypatch)
    settings, records = multi_inputs()
    trace = concurrent_trace(
        records,
        settings,
        period=360,
        seed=7,
        aliases={name: name for name in settings.functions},
    )
    records += [
        {
            "node": "cloud",
            "replicaVector": dict.fromkeys(trace.functions, 7),
            "rates": rates,
            "trials": [],
        }
        for rates in cloud_capacity_requirements(trace, "cloud")
    ]
    assert trace.phase_ids is not None
    assert isinstance(task.profile, CalibrationProfile)
    local = task.profile.model_dump(by_alias=True)
    local["functions"] = [
        {
            **deepcopy(local["functions"][0]),
            "function": name,
            "memoryMiB": 64,
            "serviceSeconds": 0.1,
            "statistics": {**local["functions"][0]["statistics"], "meanSeconds": 0.1},
        }
        for name in settings.functions
    ]
    profile = CalibrationProfile.model_validate(local)
    task.profile = CalibrationProfiles(
        {node.id: profile for node in settings.nodes},
        "a" * 64,
        profile.environment_fingerprint,
        concurrent_capacities=records,
    )
    timing = TimingSettings.model_validate(
        {
            "periodCandidatesSeconds": [360],
            "maxTraceResolutionSeconds": 360,
            "minSamples": 50,
            "quantile": 0.95,
            "epsilon": 0.05,
            "measurementLeadSeconds": 2,
            "protocol": {"preparationSeconds": 1},
            "matrix": [
                {"id": phase, "rates": window}
                for phase, window in zip(trace.phase_ids, trace.windows, strict=True)
            ],
        }
    )
    task.settings = settings.model_copy(
        update={"timing": timing, "profile": task.settings.profile}
    )
    task.topology.resources.nodes = {
        node.id: SimpleNamespace(config=node) for node in settings.nodes
    }
    task.topology.endpoints = {node.id: "http://" + node.id for node in settings.nodes}
    task.topology.function_settings = settings.functions
    task.topology.function_aliases = {name: name for name in settings.functions}
    task.topology.node_function_settings = {
        node.id: resolve_node_functions(settings, node.id) for node in settings.nodes
    }
    allocations = []
    cgroups = []

    def sample_cgroup(*args):
        cgroups.append(True)
        return {
            "memory.max": str(64 * 1024 * 1024),
            "memory.swap.max": "0",
            "memory.current": "1024",
            "memory.peak": "2048",
            "cpu.max": "21400 100000",
            "memory.events": "oom 1\noom_kill 1" if oom else "oom 0\noom_kill 0",
        }

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.preflight.collect_container_cgroup", sample_cgroup
    )

    def response(request):
        if request.method == "PUT":
            allocations.append(json.loads(request.content)["replicas"])
            return httpx.Response(200)
        return httpx.Response(
            200, json={"maxConcurrentHandlers": 1, "physicalReleaseProof": True}
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.qualification.httpx.Client",
            lambda **kwargs: nullcontext(http),
        )
        monkeypatch.setattr(
            "nanolab.tasks.one_shot.qualification.runtime_endpoint",
            lambda *args, **kwargs: (
                "http://sdk",
                {"HostConfig": {"MemorySwap": 64 * 1024 * 1024}},
            ),
        )
        if oom:
            with pytest.raises(ValueError, match="OOM"):
                task.run(inputs)
            assert not (tmp_path / "timing.json").exists()
            return
        result = task.run(inputs).value
    assert result is not None and result.qualified
    assert result.period_seconds == 360 and result.sample_count == 450
    assert len(result.matrix) == 9 and all(
        cast(Any, cell)["result"]["sample_count"] == 50 for cell in result.matrix
    )
    assert allocations == [7] * 5
    assert len(cgroups) == 35


def test_censored_matrix_is_retained_and_never_qualified(tmp_path, monkeypatch):
    task, inputs, calls = task_fixture(tmp_path, monkeypatch, censored=True)
    with pytest.raises(ValueError, match="NOT_QUALIFIED"):
        task.run(inputs)
    result = TimingQualification.model_validate_json(
        (tmp_path / "timing.json").read_bytes()
    )
    assert not result.qualified and result.censored_count == 20
    assert [row[0] for row in calls if row[1] == "diagnostics"] == list(range(20))
    assert result.samples_seconds == [None] * 20
    assert len((tmp_path / "matrix.jsonl").read_text().splitlines()) == 20


def test_unconfirmed_replica_release_does_not_publish_success(tmp_path, monkeypatch):
    task, inputs, _calls = task_fixture(tmp_path, monkeypatch, release=False)
    with pytest.raises(RuntimeError, match="release unconfirmed"):
        task.run(inputs)
    assert not (tmp_path / "timing.json").exists()
    assert len((tmp_path / "matrix.jsonl").read_text().splitlines()) == 20


def test_censored_transition_diagnostics_keep_container_identity_and_lookup_failure(
    tmp_path, monkeypatch
):
    from nanolab.tasks.one_shot.qualification import capture_replica_diagnostics

    def lookup(node, name, **kwargs):
        if kwargs["replica"] == 2:
            raise RuntimeError("replica missing")
        return "http://runtime", {"State": {"StartedAt": "timestamp"}}

    monkeypatch.setattr("nanolab.tasks.one_shot.qualification.runtime_endpoint", lookup)

    topology = SimpleNamespace(
        resources=SimpleNamespace(nodes={"edge-0": object()}),
        function_settings={"work": SimpleNamespace(max_replicas=2)},
    )
    path = tmp_path / "replicas.jsonl"
    capture_replica_diagnostics(topology, object(), path, epoch=10)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["container"]["State"]["StartedAt"] == "timestamp"
    assert rows[1]["error"] == "replica missing"
    assert all(row["epoch"] == 10 for row in rows)


def test_qualification_freezes_owned_loaded_scope(tmp_path, monkeypatch):
    task, inputs, calls = task_fixture(tmp_path, monkeypatch)

    class Load:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            calls.append(("load", "begin"))
            return self

        def __exit__(self, *args):
            calls.append(("load", "close"))

        def require_healthy(self):
            calls.append(("load", "health"))

        def audit(self, **kwargs):
            return {
                "valid": True,
                "emitted": 100,
                "physicalCompletions": 100,
                "method": "owned-http-sdk-contention-v1",
            }

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.qualification.QualificationLoad", Load, raising=False
    )
    result = task.run(inputs).value
    assert result is not None and result.qualified
    assert all(cast(dict, row["load"])["valid"] for row in result.matrix)
    assert calls.count(("load", "begin")) == calls.count(("load", "close")) == 2
    assert calls.count(("load", "health")) >= 20
