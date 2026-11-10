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
from nanolab.tasks.resource_tasks import resource_of


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
    with pytest.raises(ValueError, match="predates the workbench"):
        require_resource_overhead(settings)


def _gate(tmp_path, policy):
    import hashlib
    from types import SimpleNamespace

    from nanolab.config.one_shot import ArtifactReference

    path = tmp_path / "gate.json"
    path.write_text(json.dumps({"qualified": True, "policy": policy}))
    return SimpleNamespace(
        strict_memory=True,
        purpose="scientific-experiment",
        functions={"a": {}, "b": {}},
        observability=SimpleNamespace(
            vm_resource_overhead=ArtifactReference(
                path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest()
            )
        ),
    )


def test_sampler_era_overhead_qualification_must_be_requalified(tmp_path):
    from nanolab.one_shot.vm_resources import (
        LEGACY_SAMPLER_POLICY,
        require_resource_overhead,
    )

    with pytest.raises(ValueError, match=r"predates the workbench.*re-qualif"):
        require_resource_overhead(_gate(tmp_path, LEGACY_SAMPLER_POLICY))


def test_overhead_qualification_compares_the_export_policy(tmp_path):
    from nanolab.one_shot.vm_resources import require_resource_overhead

    exported = {**telemetry_policy(), "exporterImages": {"cadvisor": "img@sha256:1"}}
    # The policy matches (images are bound by the package fingerprint), so the
    # next check, the package fingerprint, is the one that refuses it.
    with pytest.raises(ValueError, match="collector scope changed"):
        require_resource_overhead(_gate(tmp_path, exported))
    changed = {**telemetry_policy(), "periodSeconds": 1}
    with pytest.raises(ValueError, match="export policy changed"):
        require_resource_overhead(_gate(tmp_path, changed))


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
def test_timing_load_exports_its_resources_and_preserves_auction_error(
    tmp_path, monkeypatch, logical
):
    from types import SimpleNamespace

    from nanolab.tasks.one_shot.qualification_load import QualificationLoad

    actions = []
    monkeypatch.setattr(
        "nanolab.tasks.one_shot.qualification_load.capacity_telemetry_enabled",
        lambda _: True,
    )

    def export(run, *, coverage_until):
        assert run.run_dir == tmp_path / "timing"
        assert coverage_until is not None
        actions.append("resources")

    monkeypatch.setattr(
        "nanolab.tasks.one_shot.experiment.export_run_resources", export
    )
    child = SimpleNamespace(poll=lambda: None)
    resource = resource_of(
        "generator",
        lambda _: actions.append("generator-start") or child,
        lambda *_: actions.append("generator-stop"),
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
        platform=SimpleNamespace(loadgen=lambda **_: resource),
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
    # The trial window is exported after the generator stopped, as the
    # sampler closed; the supervisor targets replace its health checks.
    assert actions.index("generator-stop") < actions.index("resources")
    assert "terminal-clock" not in actions
    if logical:
        manifest = json.loads((tmp_path / "timing/manifest.json").read_text())
        assert manifest["parameters"]["logicalSchedule"]["protocolVersion"] == 2
        assert manifest["parameters"]["logicalSchedule"]["probeScope"] == "parent"


def test_protocol_rejects_changed_vm_sampling_period(tmp_path, monkeypatch):
    from nanolab.one_shot.models import CampaignProtocol
    from tests.one_shot.test_synchronized_campaign import protocol_fixture

    protocol, _, _ = protocol_fixture(tmp_path, monkeypatch)
    protocol["analysis"]["vmTelemetry"]["periodSeconds"] += 1
    with pytest.raises(ValueError, match="telemetry"):
        CampaignProtocol.model_validate(protocol)


def test_a_sampler_era_campaign_freeze_must_be_refrozen(tmp_path, monkeypatch):
    from nanolab.one_shot.models import CampaignProtocol
    from nanolab.one_shot.vm_resources import LEGACY_SAMPLER_POLICY
    from tests.one_shot.test_synchronized_campaign import protocol_fixture

    protocol, _, _ = protocol_fixture(tmp_path, monkeypatch)
    protocol["analysis"]["vmTelemetry"] = LEGACY_SAMPLER_POLICY
    with pytest.raises(ValueError, match=r"predates the workbench.*re-freeze"):
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


def test_one_second_policy_summary_unchanged(tmp_path):
    """A 1 s run summarizes to exactly the JSON the per-second slots produced."""
    import hashlib

    anchor = datetime(2026, 10, 10, tzinfo=UTC)
    directory = tmp_path / "vm-resources"
    directory.mkdir()
    policy = {"schemaVersion": 1, "periodSeconds": 1, "samplerSha256": "0" * 64}
    (directory / "policy.json").write_text(json.dumps(policy))
    memory = {
        "memoryTotalBytes": 1000,
        "memoryAvailableBytes": 400,
        "memoryUsedBytes": 600,
        "swapTotalBytes": 0,
        "swapFreeBytes": 0,
        "swapUsedBytes": 0,
        "vcpuCount": 2,
    }
    rows = [
        {
            "timestampUtc": (
                anchor + timedelta(seconds=second, milliseconds=300)
            ).isoformat(),
            "node": "edge1",
            **memory,
            "cpuUtilization": None if second == -2 else 0.5,
            "errors": [{"containerId": "c2", "error": "gone"}] if second == 3 else [],
            "expectedContainerIds": ["c1", "c2"],
            "containers": [{"containerId": "c1", "role": "function"}],
        }
        for second in [-2, -1, 0, 0, 2, 3]
    ]
    (directory / "edge1.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows) + "{partial"
    )
    result = summarize_resources(
        tmp_path,
        anchor=anchor,
        warmup=2,
        period=2,
        epochs=2,
        nodes=["edge1", "cloud"],
        mode="ewma",
        functions=["a"],
        run_id="r",
    )
    assert result["nodes"]["edge1"]["missingSeconds"] == [1]
    assert result["nodes"]["edge1"]["duplicateSeconds"] == [0]
    # Hash of the summary as produced before slots read periodSeconds (8b5006d7).
    assert (
        hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        == "3e56866cd45cefee3930c418b18be5b424e750844756a6ac5982f897efac2e3e"
    )
