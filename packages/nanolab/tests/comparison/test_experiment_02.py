"""Experiment 02 runs every configuration under every budget, in a seeded order."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from nanolab.comparison.experiment_01 import (
    EVENTS,
    SUMMARY,
    attempt_dir,
    read_cell_result,
    write_cell_result,
)
from nanolab.comparison.experiment_02 import (
    CONFIGURATIONS_BY_KEY,
    EXPERIMENT_02_CONFIGURATIONS,
    EXPERIMENT_02_EVIDENCE,
    SAMPLES,
    Experiment02Cell,
    build_experiment_02_matrix,
    experiment_02_identity,
)
from nanolab.comparison.manifest import (
    capture_comparison_inputs,
    new_comparison_manifest,
)
from nanolab.config.environment import EnvironmentConfig, RoleTarget
from nanolab.config.experiment_02 import Budget
from nanolab.config.scenario import ScenarioConfig
from nanolab.workspace.paths import discover_tool_root

SCENARIOS = discover_tool_root() / "scenarios"
BUDGETS = (
    Budget(cpus=1, memoryMiB=1024),
    Budget(cpus=2, memoryMiB=2048),
    Budget(cpus=4, memoryMiB=4096),
)
P1_JVM = CONFIGURATIONS_BY_KEY["nanofaas-p1-jvm"]
IDENTITY = {"tag": "recipe-test", "experiment": {"profile": "experiment-02"}}


# ---------------------------------------------------------------- matrix


def test_matrix_runs_every_pair_once_per_repetition():
    cells = build_experiment_02_matrix(
        EXPERIMENT_02_CONFIGURATIONS[:2], BUDGETS[:2], 3, seed=7
    )
    assert [cell.repetition for cell in cells] == [1] * 4 + [2] * 4 + [3] * 4
    for repetition in (1, 2, 3):
        pairs = {
            (cell.key, cell.budget.label)
            for cell in cells
            if cell.repetition == repetition
        }
        assert pairs == {
            ("nanofaas-p1-jvm", "cpu1-mem1024"),
            ("nanofaas-p1-jvm", "cpu2-mem2048"),
            ("nanofaas-p1-native", "cpu1-mem1024"),
            ("nanofaas-p1-native", "cpu2-mem2048"),
        }


def test_matrix_order_is_reproducible_and_reshuffled_per_repetition():
    first = build_experiment_02_matrix(EXPERIMENT_02_CONFIGURATIONS, BUDGETS, 2, seed=7)
    assert first == build_experiment_02_matrix(
        EXPERIMENT_02_CONFIGURATIONS, BUDGETS, 2, seed=7
    )
    one = [(cell.key, cell.budget.label) for cell in first if cell.repetition == 1]
    two = [(cell.key, cell.budget.label) for cell in first if cell.repetition == 2]
    assert one != two
    assert sorted(one) == sorted(two)
    other = build_experiment_02_matrix(EXPERIMENT_02_CONFIGURATIONS, BUDGETS, 2, seed=8)
    assert [cell.label for cell in other] != [cell.label for cell in first]


def test_matrix_refuses_an_empty_grid():
    with pytest.raises(ValueError, match="repetition"):
        build_experiment_02_matrix(EXPERIMENT_02_CONFIGURATIONS, BUDGETS, 0, seed=1)
    with pytest.raises(ValueError, match="configuration"):
        build_experiment_02_matrix((), BUDGETS, 1, seed=1)
    with pytest.raises(ValueError, match="budget"):
        build_experiment_02_matrix(EXPERIMENT_02_CONFIGURATIONS, (), 1, seed=1)


def test_cell_directory_and_label(tmp_path):
    cell = Experiment02Cell(P1_JVM, Budget(cpus=2, memoryMiB=2048), 1)
    assert cell.key == "nanofaas-p1-jvm"
    assert cell.label == "nanofaas-p1-jvm cpu2-mem2048 run 1"
    assert (
        cell.run_dir(tmp_path)
        == tmp_path / "nanofaas-p1-jvm" / "cpu2-mem2048" / "run-1"
    )


# ---------------------------------------------------------------- receipts


def _receipt(root: Path, cell: Experiment02Cell, outcome: str = "measured"):
    return write_cell_result(
        cell,
        root,
        identity=IDENTITY,
        attempt=1,
        outcome=outcome,  # type: ignore[arg-type]
        reason="test",
        started_at=datetime.now(UTC),
        required=EXPERIMENT_02_EVIDENCE,
    )


def test_measured_cell_requires_the_cgroup_samples(tmp_path):
    cell = Experiment02Cell(P1_JVM, BUDGETS[1], 1)
    selected = attempt_dir(cell.run_dir(tmp_path), 1)
    selected.mkdir(parents=True)
    (selected / SUMMARY).write_text("{}")
    (selected / EVENTS).write_text("")
    with pytest.raises(ValueError, match="cgroup-samples"):
        _receipt(tmp_path, cell)


def test_receipt_round_trip_with_cgroup_samples(tmp_path):
    cell = Experiment02Cell(P1_JVM, BUDGETS[1], 1)
    selected = attempt_dir(cell.run_dir(tmp_path), 1)
    (selected / "metrics").mkdir(parents=True)
    (selected / SUMMARY).write_text("{}")
    (selected / EVENTS).write_text("")
    (selected / SAMPLES).write_text('{"ts": 0, "cgroups": {}}\n')
    receipt = _receipt(tmp_path, cell)
    assert receipt["variant"] == "nanofaas-p1-jvm"
    assert receipt["notProduced"] == []
    assert read_cell_result(cell, tmp_path, IDENTITY) == receipt
    (selected / SAMPLES).write_text("changed\n")
    with pytest.raises(ValueError, match="changed"):
        read_cell_result(cell, tmp_path, IDENTITY)


def test_failed_cell_names_the_evidence_it_could_not_produce(tmp_path):
    cell = Experiment02Cell(P1_JVM, BUDGETS[1], 1)
    diagnostics = attempt_dir(cell.run_dir(tmp_path), 1) / "diagnostics"
    diagnostics.mkdir(parents=True)
    (diagnostics / "error.txt").write_text("readiness timed out\n")
    receipt = _receipt(tmp_path, cell, outcome="startup_failed")
    assert receipt["notProduced"] == [
        "k6-summary.json",
        "k6-events.jsonl",
        "metrics/cgroup-samples.jsonl",
    ]


# ---------------------------------------------------------------- manifest


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)


def _repositories(tmp_path: Path) -> tuple[Path, Path]:
    roots = []
    for name in ("nanofaas", "nanolab"):
        root = tmp_path / name
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "tests@example.com")
        _git(root, "config", "user.name", "Tests")
        (root / "tracked").write_text("initial")
        if name == "nanofaas":
            function = root / "functions" / "rust" / "qr-code"
            function.mkdir(parents=True)
            (function / "function.yaml").write_text(
                "name: qr-code-rust\ncatalog:\n  defaultImage: test/rust:latest\n"
            )
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "initial")
        roots.append(root)
    return roots[0], roots[1]


SELECTED = ("nanofaas-p1-jvm", "direct-reference")


def _capture(tmp_path: Path, *, order: list[str] | None = None):
    nanofaas, nanolab = _repositories(tmp_path)
    scenario = ScenarioConfig.model_validate(
        yaml.safe_load((SCENARIOS / "experiment-02-pilot.yaml").read_text())
    )
    protocol = scenario.experiment_02
    assert protocol is not None
    cells = build_experiment_02_matrix(
        tuple(CONFIGURATIONS_BY_KEY[key] for key in SELECTED),
        tuple(protocol.budgets),
        1,
        seed=protocol.order_seed,
    )
    script = tmp_path / "experiment-02.js"
    script.write_text("// generator\n")
    experiment = experiment_02_identity(
        protocol,
        cells,
        payload=b"{}",
        script=script,
        selected=SELECTED,
        seed=protocol.order_seed,
    )
    if order is not None:
        experiment["order"] = order
    recipe = tmp_path / "experiment-02-p1-jvm.yaml"
    recipe.write_text("controlPlane: {}\n")
    inputs = capture_comparison_inputs(
        scenario=scenario,
        environment=EnvironmentConfig(
            provider="multipass", roles={"stack": RoleTarget(name="experiment")}
        ),
        nanofaas_root=nanofaas,
        nanolab_root=nanolab,
        profiles={"nanofaas-p1-jvm": recipe},
        variants=SELECTED,
        repetitions=1,
        tag="recipe-test",
        build_memory=None,
        parallelism=None,
        experiment=experiment,
        profile_options=lambda _key, _path: {
            "mode": "jvm",
            "variant": None,
            "modules": [
                "async-queue",
                "build-metadata",
                "container-deployment-provider",
            ],
            "jvm_args": ["-XX:+UseSerialGC", "-Xmx64m"],
            "native": None,
            "functions": [],
        },
    )
    return inputs, cells


def test_manifest_accepts_an_experiment_02_identity(tmp_path):
    inputs, cells = _capture(tmp_path)
    manifest = new_comparison_manifest(
        inputs, cells, variants=[{"key": key} for key in SELECTED]
    )
    assert manifest.order == [cell.label for cell in cells]
    assert manifest.variants == [
        {"key": "nanofaas-p1-jvm"},
        {"key": "direct-reference"},
    ]


def test_manifest_rejects_an_order_of_the_wrong_length(tmp_path):
    with pytest.raises(ValueError, match="experiment artifact identity"):
        _capture(tmp_path, order=["nanofaas-p1-jvm cpu2-mem2048 run 1"])
