from pathlib import Path

import pytest

from nanolab.comparison.profiles import experiment_01_profiles
from nanolab.images.control_plane_variants import VARIANTS_BY_KEY, runtime_class

ROOT = Path(__file__).parents[2] / "src/nanolab/assets/presets"
KEYS = (
    "jvm",
    "jvm-g1",
    "jvm-c2",
    "jvm-g1-c2",
    "native-os",
    "native-o3",
    "native-o3-g1",
)


def test_runtime_recipes_share_one_qr_publisher():
    profiles = experiment_01_profiles(ROOT, KEYS)
    assert tuple(profiles) == KEYS
    assert all(p.name == f"experiment-01-runtime-{k}.yaml" for k, p in profiles.items())


def test_native_os_is_not_o3():
    assert runtime_class(VARIANTS_BY_KEY["native-os"]) == "native-os"


def test_mixed_groups_rejected():
    with pytest.raises(ValueError, match="mixed"):
        experiment_01_profiles(ROOT, ("jvm", "experiment-01-core-k8s-jvm-jit"))


def test_gc_phase_preserves_missing_and_zero():
    from nanolab.comparison.experiment_01 import gc_phase

    assert gc_phase(None, 0, 10)["jvm_gc_collection_count"] is None
    snapshot = {
        "queries": {
            "jvm_gc_collection_count": {
                "points": [
                    {"timestamp": "1970-01-01T00:00:00+00:00", "value": 3},
                    {"timestamp": "1970-01-01T00:00:10+00:00", "value": 3},
                ]
            }
        }
    }
    assert gc_phase(snapshot, 0, 10)["jvm_gc_collection_count"] == 0


def test_offline_runtime_report_lists_incomplete_cells(tmp_path):
    import yaml

    from nanolab.comparison.experiment_01 import write_runtime_gc_html
    from nanolab.comparison.matrix import ComparisonCell
    from nanolab.config.scenario import ScenarioConfig

    scenario = ScenarioConfig.model_validate(
        yaml.safe_load(
            (ROOT / "scenarios/experiment-01-runtime-gc-pilot-cpu1.yaml").read_text()
        )
    )
    cell = ComparisonCell(VARIANTS_BY_KEY["jvm"], 1)
    summary = {"variant": "jvm", "repetition": 1, "outcome": "incomplete", "phases": []}
    assert scenario.experiment_01 is not None
    write_runtime_gc_html(tmp_path, (cell,), [summary], scenario.experiment_01)
    html = (tmp_path / "experiment-01-report.html").read_text()
    assert "incomplete" in html
    assert '<script src="https://cdn.plot.ly' not in html


def test_cli_regenerates_e01_without_sources(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import typer
    import yaml
    from typer.testing import CliRunner

    from nanolab.cli import comparison
    from nanolab.comparison.matrix import ComparisonCell

    protocol = yaml.safe_load(
        (ROOT / "scenarios/experiment-01-runtime-gc-pilot-cpu1.yaml").read_text()
    )["experiment01"]
    cell = ComparisonCell(VARIANTS_BY_KEY["jvm"], 1)
    manifest = SimpleNamespace(
        identity={
            "variants": ["jvm"],
            "experiment": {
                "profile": "experiment-01",
                "protocol": protocol,
                "builds": {"jvm": {}},
                "order": [cell.label],
            },
        },
        repetitions=1,
    )
    monkeypatch.setattr(comparison, "read_comparison_manifest", lambda root: manifest)
    app = typer.Typer()
    comparison.register(app)
    result = CliRunner().invoke(app, ["compare-report", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "experiment-01-summary.json").exists()
    assert (tmp_path / "experiment-01-report.html").exists()


# Reuse the existing immutable-source fixture instead of another manifest builder.
from tests.comparison.test_experiment_01 import experiment_inputs  # noqa: F401, E402


def test_saved_measured_runtime_report_is_reproducible_and_rejects_tampering(
    tmp_path, request
):
    import json
    from datetime import UTC, datetime

    import typer
    import yaml
    from typer.testing import CliRunner

    from nanolab.cli import comparison
    from nanolab.comparison.cells import (
        EVENTS,
        SNAPSHOT,
        SUMMARY,
        attempt_dir,
        write_cell_result,
    )
    from nanolab.comparison.experiment_01 import experiment_01_identity
    from nanolab.comparison.manifest import (
        new_comparison_manifest,
        write_comparison_manifest,
    )
    from nanolab.comparison.matrix import build_experiment_01_matrix
    from nanolab.comparison.profiles import contract_for
    from nanolab.config.experiment_01 import Experiment01Protocol
    from nanolab.images.control_plane_variants import (
        experiment_01_catalog,
        resolve_variants,
    )

    capture, _ = request.getfixturevalue("experiment_inputs")
    protocol = Experiment01Protocol.model_validate(
        yaml.safe_load(
            (ROOT / "scenarios/experiment-01-runtime-gc-pilot-cpu1.yaml").read_text()
        )["experiment01"]
    )
    keys = ("native-o3",)
    cells = build_experiment_01_matrix(
        resolve_variants(keys), 1, catalog=tuple(experiment_01_catalog(keys).values())
    )
    payload = (ROOT / "scenarios/payloads" / protocol.payload_file).read_bytes()
    experiment = experiment_01_identity(
        protocol,
        cells,
        payload=payload,
        script=ROOT.parent / "k6/experiment-01.js",
        selected=keys,
    )
    inputs = capture(
        variants=keys,
        repetitions=1,
        profiles=experiment_01_profiles(ROOT, keys),
        experiment=experiment,
        profile_options=contract_for({"experiment": experiment}).options,
    )
    root = tmp_path / "archive"
    manifest = new_comparison_manifest(inputs, cells)
    write_comparison_manifest(root, manifest)
    cell = cells[0]
    attempt = attempt_dir(cell.run_dir(root), 1)
    (attempt / "metrics").mkdir(parents=True)
    events = [
        {"t": "origin", "ts": 1000000},
        {"t": "send", "ts": 1121000, "id": "1", "phase": "step-01"},
        {
            "t": "done",
            "ts": 1121010,
            "id": "1",
            "phase": "step-01",
            "ok": True,
            "status": 200,
            "dur": 10,
        },
    ]
    (attempt / EVENTS).write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (attempt / SUMMARY).write_text('{"metrics": {}}')
    points = [
        {"timestamp": datetime.fromtimestamp(t, UTC).isoformat(), "value": v}
        for t, v in ((1000, 3), (1010, 4))
    ]
    (attempt / SNAPSHOT).write_text(
        json.dumps(
            {
                "start": datetime.fromtimestamp(995, UTC).isoformat(),
                "queries": {"jvm_gc_collection_count": {"points": points}},
            }
        )
    )
    write_cell_result(
        cell,
        root,
        identity=manifest.identity,
        attempt=1,
        outcome="measured",
        reason="test",
        started_at=datetime.now(UTC),
    )
    for name in ("nanofaas", "nanolab"):
        (tmp_path / name).rename(tmp_path / (name + "-unavailable"))
    app = typer.Typer()
    comparison.register(app)
    runner = CliRunner()
    result = runner.invoke(app, ["compare-report", str(root)])
    assert result.exit_code == 0, result.output
    import csv

    with (root / "experiment-01-summary.csv").open() as stream:
        csv_rows = list(csv.DictReader(stream))
    assert csv_rows[0]["jvm_gc_collection_count"] == "1.0"
    first = (root / "experiment-01-summary.json").read_bytes()
    rows = json.loads(first)
    assert rows[0]["phases"][0]["gc"]["jvm_gc_collection_count"] == 1
    assert (root / "experiment-01-report.html").is_file()
    html = (root / "experiment-01-report.html").read_text()
    for title in (
        "Scheduled arrivals",
        "Emitted requests",
        "Completed HTTP responses",
        "Cohort p95 latency",
        "Queue depth",
        "CPU throttling",
    ):
        assert title in html

    assert runner.invoke(app, ["compare-report", str(root)]).exit_code == 0
    assert (root / "experiment-01-summary.json").read_bytes() == first
    (attempt / SNAPSHOT).write_text("{}")
    result = runner.invoke(app, ["compare-report", str(root)])
    assert result.exit_code != 0
    assert "evidence missing or changed" in result.output


def test_gc_phase_keeps_publishers_separate_when_one_counter_resets():
    from nanolab.comparison.experiment_01 import gc_phase

    points = [
        {
            "timestamp": f"1970-01-01T00:00:{second:02d}+00:00",
            "value": value,
            "labels": {"gc": collector},
        }
        for second, collector, value in (
            (0, "young", 4),
            (0, "full", 8),
            (10, "young", 7),
            (10, "full", 1),
        )
    ]
    values = gc_phase(
        {"queries": {"jvm_gc_collection_count": {"points": points}}}, 0, 10
    )
    assert values["jvm_gc_collection_count"] == 4
    assert values["jfr_gc_operation_time"] is None


def test_missing_generator_origin_does_not_plot_resource_time_as_idle_time(
    tmp_path, monkeypatch
):
    import json

    import yaml

    from nanolab.comparison.cells import EVENTS, SNAPSHOT, attempt_dir
    from nanolab.comparison.experiment_01 import write_runtime_gc_html
    from nanolab.comparison.matrix import ComparisonCell
    from nanolab.config.experiment_01 import Experiment01Protocol
    from nanolab.tasks.loadtest import comparison_report

    protocol = Experiment01Protocol.model_validate(
        yaml.safe_load(
            (ROOT / "scenarios/experiment-01-runtime-gc-pilot-cpu1.yaml").read_text()
        )["experiment01"]
    )
    cell = ComparisonCell(VARIANTS_BY_KEY["jvm"], 1)
    folder = attempt_dir(cell.run_dir(tmp_path), 1)
    (folder / "metrics").mkdir(parents=True)
    (folder / EVENTS).write_text("")
    metric = "container_memory_bytes@control-plane"
    (folder / SNAPSHOT).write_text(
        json.dumps(
            {
                "start": "1970-01-01T00:16:35+00:00",
                "queries": {
                    metric: {
                        "points": [
                            {"timestamp": "1970-01-01T00:16:40+00:00", "value": 123456},
                        ]
                    }
                },
            }
        )
    )
    observed = {}
    render = comparison_report._over_time

    def chart(data, labels, name, *args):
        observed[name] = data[0].series[name]
        return render(data, labels, name, *args)

    monkeypatch.setattr(comparison_report, "_over_time", chart)
    write_runtime_gc_html(
        tmp_path,
        (cell,),
        [
            {
                "variant": "jvm",
                "repetition": 1,
                "outcome": "measured",
                "attempt": 1,
                "phases": [],
            }
        ],
        protocol,
    )
    assert observed[metric].empty


def test_runtime_queries_retain_all_jfr_operation_times_for_offline_analysis():
    from nanolab.metrics.catalogue import runtime_queries

    query = next(
        (
            q
            for q in runtime_queries("qr-code-rust")
            if q.name == "jfr_vm_operation_time"
        ),
        None,
    )
    assert query is not None
    assert (
        query.expr
        == 'nanofaas_jfr_vm_operation_time_seconds_total{app="nanofaas-control-plane"}'
    )
    assert not query.required
