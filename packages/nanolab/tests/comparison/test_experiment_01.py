"""Experiment 01: builds, order, definitive outcomes, ledger and verdicts."""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from nanolab.comparison.experiment_01 import (
    CELL_RESULT,
    attempt_dir,
    build_ledger,
    classify_failure,
    experiment_completed,
    next_attempt,
    read_cell_result,
    read_events,
    summarize_phases,
    write_cell_result,
    write_experiment_01_report,
)
from nanolab.comparison.matrix import (
    EXPERIMENT_01_ORDERS,
    build_experiment_01_matrix,
    build_matrix,
    pending,
)
from nanolab.comparison.profiles import (
    EXPERIMENT_01_CONTRACT,
    contract_for,
    experiment_01_options,
    experiment_01_profiles,
)
from nanolab.config.experiment_01 import Experiment01Protocol
from nanolab.images.control_plane_variants import (
    EXPERIMENT_01_VARIANTS,
    EXPERIMENT_01_VARIANTS_BY_KEY,
    VARIANTS,
    jvm_optimization,
    resolve_variants,
    runtime_class,
)
from nanolab.plans.runtime_comparison import (
    EXPERIMENT_01_SCRIPT_NAME,
    experiment_01_k6_environment,
    script_for,
)
from nanolab.workspace.paths import discover_tool_root

TOOL_ROOT = discover_tool_root()
KEYS = tuple(EXPERIMENT_01_VARIANTS_BY_KEY)
JVM_KEYS = tuple(
    key
    for key in KEYS
    if not str(EXPERIMENT_01_VARIANTS_BY_KEY[key].runtime).startswith("native")
)
EXPECTED = {
    "experiment-01-core-k8s-jvm-jit": (
        ["build-metadata", "k8s-deployment-provider"],
        "tiered",
    ),
    "experiment-01-core-k8s-native-o3": (
        ["build-metadata", "k8s-deployment-provider"],
        "native-o3",
    ),
    "experiment-01-core-k8s-queue-jvm-int": (
        ["async-queue", "build-metadata", "k8s-deployment-provider"],
        "interpreted",
    ),
    "experiment-01-core-k8s-queue-jvm-c1": (
        ["async-queue", "build-metadata", "k8s-deployment-provider"],
        "c1",
    ),
    "experiment-01-core-k8s-queue-jvm-jit": (
        ["async-queue", "build-metadata", "k8s-deployment-provider"],
        "tiered",
    ),
    "experiment-01-core-k8s-queue-native-o2": (
        ["async-queue", "build-metadata", "k8s-deployment-provider"],
        "native-o2",
    ),
    "experiment-01-core-k8s-queue-native-o3": (
        ["async-queue", "build-metadata", "k8s-deployment-provider"],
        "native-o3",
    ),
}
TIER = {
    "tiered": [],
    "c1": ["-XX:TieredStopAtLevel=1"],
    "interpreted": ["-Xint"],
}


def _protocol(**changes: object) -> Experiment01Protocol:
    scenario = yaml.safe_load(
        (TOOL_ROOT / "scenarios" / "experiment-01-pilot.yaml").read_text()
    )
    return Experiment01Protocol.model_validate({**scenario["experiment01"], **changes})


# ---------------------------------------------------------------- builds


@pytest.mark.parametrize("key", KEYS)
def test_each_recipe_declares_exactly_its_composition_and_runtime(key):
    variant = EXPERIMENT_01_VARIANTS_BY_KEY[key]
    path = TOOL_ROOT / "recipes" / f"{key}.yaml"
    options: dict[str, Any] = experiment_01_options(
        path, variant, shares=key == KEYS[0]
    )
    modules, runtime = EXPECTED[key]
    assert options["modules"] == modules
    assert options["runtime"] == runtime == runtime_class(variant)
    if runtime.startswith("native"):
        assert options["jvm_args"] == []
        assert options["native"]["gc"] == "serial"
        assert options["native"]["optimization"] == runtime[-1]
        assert options["native"]["maxHeap"] == "64m"
    else:
        assert options["jvm_args"] == ["-XX:+UseSerialGC", "-Xmx64m", *TIER[runtime]]
    functions = options["functions"]
    assert functions == (
        [{"name": "qr-code", "sdk": "rust", "mode": None, "image": "rust-qr-code"}]
        if key == KEYS[0]
        else []
    )


def test_only_the_first_recipe_may_declare_the_function(tmp_path):
    source = TOOL_ROOT / "recipes" / f"{KEYS[0]}.yaml"
    variant = EXPERIMENT_01_VARIANTS_BY_KEY[KEYS[0]]
    with pytest.raises(ValueError, match="first"):
        experiment_01_options(source, variant, shares=False)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda r: r["controlPlane"]["modules"].remove("build-metadata"), "modules"),
        (lambda r: r["controlPlane"]["jvm"]["args"].append("-Xmx128m"), "64m"),
        (lambda r: r["controlPlane"]["build"].update(variant="x"), "variant"),
    ],
)
def test_recipes_cannot_drift_from_the_protocol(tmp_path, edit, message):
    key = "experiment-01-core-k8s-queue-jvm-c1"
    recipe = yaml.safe_load((TOOL_ROOT / "recipes" / f"{key}.yaml").read_text())
    edit(recipe)
    path = tmp_path / f"{key}.yaml"
    path.write_text(yaml.safe_dump(recipe))
    with pytest.raises(ValueError, match=message):
        experiment_01_options(path, EXPERIMENT_01_VARIANTS_BY_KEY[key], shares=False)


@pytest.mark.parametrize(
    "edit",
    [
        lambda native: native.pop("maxHeap"),
        lambda native: native.update(maxHeap="128m"),
    ],
)
def test_native_recipes_require_the_build_time_heap(tmp_path, edit):
    key = "experiment-01-core-k8s-queue-native-o2"
    recipe = yaml.safe_load((TOOL_ROOT / "recipes" / f"{key}.yaml").read_text())
    edit(recipe["controlPlane"]["build"]["native"])
    path = tmp_path / f"{key}.yaml"
    path.write_text(yaml.safe_dump(recipe))
    with pytest.raises(ValueError, match="maxHeap 64m"):
        experiment_01_options(path, EXPERIMENT_01_VARIANTS_BY_KEY[key], shares=False)


def test_full_selection_publishes_the_function_recipe_first():
    selected = experiment_01_profiles(TOOL_ROOT, KEYS[::-1])
    assert next(iter(selected)) == KEYS[0]
    assert set(selected) == set(KEYS)


def test_runtime_class_is_separate_from_the_historical_c1_c2_label():
    for variant in EXPERIMENT_01_VARIANTS:
        with pytest.raises(ValueError, match="runtime_class"):
            jvm_optimization(variant)
    historical = {v.key: v for v in VARIANTS}
    assert {
        key: runtime_class(historical[key])
        for key in ("jvm", "jvm-g1", "jvm-c2", "jvm-g1-c2", "native-o3")
    } == {
        "jvm": "c1",
        "jvm-g1": "c1",
        "jvm-c2": "tiered",
        "jvm-g1-c2": "tiered",
        "native-o3": "native-o3",
    }
    with pytest.raises(ValueError, match="-Os"):
        runtime_class(historical["native-os"])


def test_contract_is_chosen_from_the_identity():
    assert contract_for({"experiment": {"profile": "experiment-01"}}) is (
        EXPERIMENT_01_CONTRACT
    )
    assert contract_for({}).shared == "jvm"


# ---------------------------------------------------------------- matrix


def test_three_repetitions_make_21_cells_in_the_declared_orders():
    cells = build_experiment_01_matrix(EXPERIMENT_01_VARIANTS, 3)
    assert len(cells) == len({(c.variant.key, c.repetition) for c in cells}) == 21
    letters = {key: chr(ord("A") + i) for i, key in enumerate(KEYS)}
    orders = [
        "".join(letters[c.variant.key] for c in cells if c.repetition == r)
        for r in (1, 2, 3)
    ]
    assert orders == list(EXPERIMENT_01_ORDERS)
    for key in KEYS:
        positions = {
            [c.variant.key for c in cells if c.repetition == r].index(key)
            for r in (1, 2, 3)
        }
        assert len(positions) == 3


def test_pilot_uses_the_first_order_and_extra_repetitions_cycle():
    pilot = build_experiment_01_matrix(EXPERIMENT_01_VARIANTS, 1)
    assert [c.variant.key for c in pilot] == list(KEYS)
    four = build_experiment_01_matrix(EXPERIMENT_01_VARIANTS, 4)
    assert [c.variant.key for c in four[21:]] == list(KEYS)


def test_subset_keeps_the_declared_orders_filtered():
    subset = resolve_variants((KEYS[6], KEYS[0]))
    cells = build_experiment_01_matrix(subset, 3)
    # ABCDEFG -> A G, DGBFAEC -> G A, FCEAGDB -> A G
    assert [c.variant.key for c in cells] == [
        KEYS[0],
        KEYS[6],
        KEYS[6],
        KEYS[0],
        KEYS[0],
        KEYS[6],
    ]


def test_historical_order_is_unchanged():
    cells = build_matrix(resolve_variants(("jvm", "native-o3")), 2)
    assert [c.label for c in cells] == [
        "jvm run 1",
        "native-o3 run 1",
        "jvm run 2",
        "native-o3 run 2",
    ]


# ---------------------------------------------------------------- outcomes

IDENTITY = {"tag": "recipe-test", "experiment": {"profile": "experiment-01"}}


def _cell(index=0, repetition=1):
    return build_experiment_01_matrix(EXPERIMENT_01_VARIANTS, 3)[
        (repetition - 1) * 7 + index
    ]


def _measured_attempt(root: Path, cell, attempt=1) -> Path:
    selected = attempt_dir(cell.run_dir(root), attempt)
    (selected / "metrics").mkdir(parents=True)
    (selected / "k6-summary.json").write_text("{}")
    (selected / "k6-events.jsonl").write_text("")
    (selected / "metrics/prometheus-snapshot.json").write_text("{}")
    return selected


def _receipt(root, cell, attempt, outcome):
    return write_cell_result(
        cell,
        root,
        identity=IDENTITY,
        attempt=attempt,
        outcome=outcome,
        reason="test",
        started_at=datetime.now(UTC),
    )


def test_measured_requires_its_evidence(tmp_path):
    cell = _cell()
    attempt_dir(cell.run_dir(tmp_path), 1).mkdir(parents=True)
    with pytest.raises(ValueError, match="k6-summary"):
        _receipt(tmp_path, cell, 1, "measured")
    assert not (cell.run_dir(tmp_path) / CELL_RESULT).exists()


def test_failure_requires_diagnostics_and_names_what_was_not_produced(tmp_path):
    cell = _cell()
    selected = attempt_dir(cell.run_dir(tmp_path), 1)
    selected.mkdir(parents=True)
    with pytest.raises(ValueError, match="diagnostic"):
        _receipt(tmp_path, cell, 1, "oom")
    (selected / "diagnostics").mkdir()
    (selected / "diagnostics/pods.json").write_text("{}")
    receipt = _receipt(tmp_path, cell, 1, "oom")
    assert receipt["notProduced"] == [
        "k6-summary.json",
        "k6-events.jsonl",
        "metrics/prometheus-snapshot.json",
    ]


def test_oom_is_definitive_and_resume_moves_to_the_next_cell(tmp_path):
    cells = build_experiment_01_matrix(EXPERIMENT_01_VARIANTS, 1)
    selected = attempt_dir(cells[0].run_dir(tmp_path), 1)
    (selected / "diagnostics").mkdir(parents=True)
    (selected / "diagnostics/pods.json").write_text("{}")
    _receipt(tmp_path, cells[0], 1, "oom")
    _measured_attempt(tmp_path, cells[1])
    _receipt(tmp_path, cells[1], 1, "measured")
    remaining = pending(cells, tmp_path, experiment_completed(IDENTITY))
    assert remaining == cells[2:]


def test_interrupted_attempt_without_receipt_stays_pending(tmp_path):
    cells = build_experiment_01_matrix(EXPERIMENT_01_VARIANTS, 1)
    _measured_attempt(tmp_path, cells[0])
    assert pending(cells, tmp_path, experiment_completed(IDENTITY)) == cells
    assert next_attempt(cells[0].run_dir(tmp_path)) == 2


def test_changed_evidence_or_identity_is_refused(tmp_path):
    cell = _cell()
    selected = _measured_attempt(tmp_path, cell)
    _receipt(tmp_path, cell, 1, "measured")
    receipt = read_cell_result(cell, tmp_path, IDENTITY)
    assert receipt is not None and receipt["outcome"] == "measured"
    with pytest.raises(ValueError, match="belong"):
        read_cell_result(cell, tmp_path, {**IDENTITY, "tag": "other"})
    (selected / "k6-summary.json").write_text('{"changed": true}')
    with pytest.raises(ValueError, match="changed"):
        read_cell_result(cell, tmp_path, IDENTITY)


def test_a_new_attempt_never_overwrites_a_previous_one(tmp_path):
    cell = _cell()
    _measured_attempt(tmp_path, cell, 1)
    _measured_attempt(tmp_path, cell, 2)
    _receipt(tmp_path, cell, 2, "measured")
    assert next_attempt(cell.run_dir(tmp_path)) == 3
    assert (attempt_dir(cell.run_dir(tmp_path), 1) / "k6-summary.json").is_file()


def _pods(**status: object) -> dict:
    return {
        "items": [
            {
                "metadata": {"name": "nanofaas-control-plane-abc"},
                "status": {"containerStatuses": [{"name": "control-plane", **status}]},
            }
        ]
    }


@pytest.mark.parametrize(
    ("status", "load_started", "expected"),
    [
        (
            {"lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}},
            True,
            "oom",
        ),
        (
            {"state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}},
            False,
            "oom",
        ),
        (
            {"state": {"waiting": {"reason": "CrashLoopBackOff"}}},
            False,
            "startup_failed",
        ),
        ({"state": {"waiting": {"reason": "CrashLoopBackOff"}}}, True, None),
        ({"state": {"waiting": {"reason": "ImagePullBackOff"}}}, False, None),
        ({"ready": True, "state": {"running": {}}}, False, "transport"),
        ({"ready": True, "state": {"running": {}}}, True, None),
    ],
)
def test_failures_are_classified_from_the_pod_listing(status, load_started, expected):
    assert classify_failure(_pods(**status), load_started=load_started) == expected


def test_no_control_plane_pod_is_unclassified():
    assert classify_failure({"items": []}, load_started=False) is None


# ---------------------------------------------------------------- ledger

ORIGIN = 1_000_000.0


def _send(phase: str, at: float, n: int) -> list[dict]:
    """`n` sends spread evenly over one second starting at `at` seconds."""
    return [
        {
            "t": "send",
            "id": f"{phase}-{at}-{i}",
            "phase": phase,
            "ts": ORIGIN + (at + i / n) * 1000,
        }
        for i in range(n)
    ]


def _done(events: list[dict], dur: float = 10.0, **extra) -> list[dict]:
    return [
        {
            "t": "done",
            "id": e["id"],
            "phase": e["phase"],
            "ts": e["ts"] + dur,
            "dur": dur,
            "status": 200,
            "ok": True,
            **extra,
        }
        for e in events
    ]


def _calendar(rates: dict[str, float], protocol: Experiment01Protocol) -> list[dict]:
    """Events sending each phase at its given rate, all served quickly."""
    events: list[dict] = [{"t": "origin", "ts": ORIGIN}]
    for phase in protocol.phases():
        rate = rates.get(phase["name"], phase["rate"])
        for second in range(int(phase["durationSeconds"])):
            sends = _send(phase["name"], phase["startSeconds"] + second, int(rate))
            events += sends + _done(sends)
    return events


GENERATOR = {
    "maxRateDeviation": 0.05,
    "maxDroppedFraction": 0.01,
    "windowSeconds": 10,
    "maxStartDelaySeconds": 2,
}
NO_DROPS = {
    "metrics": {
        "dropped_iterations": {"count": 0},
        **{
            f"dropped_iterations{{scenario:{name}}}": {"count": 0}
            for name in ("warmup", "step-01", "step-02", "step-03", "step-04")
        },
    }
}


def _rows(events, protocol, summary=NO_DROPS, malformed=0):
    rows = summarize_phases(
        protocol,
        build_ledger(events),
        summary=summary,
        snapshot=None,
        malformed_events=malformed,
    )
    return {row["phase"]: row for row in rows}


def test_delivered_and_fully_correct_step_is_sustained():
    protocol = _protocol(status="frozen", generator=GENERATOR)
    rows = _rows(_calendar({}, protocol), protocol)
    assert rows["step-01"]["load"]["verdict"] == "delivered"
    assert rows["step-01"]["sustainability"] == "sustained"
    assert rows["idle"]["sent"] == 0 and "load" not in rows["idle"]


def test_rate_not_delivered_is_not_qualifiable_despite_full_success():
    # Requested 1000/s, sent 200/s, every response correct: the 1000/s claim
    # is never made, and the observed throughput is kept.
    protocol = _protocol(
        status="frozen",
        generator=GENERATOR,
        steps=[{"rate": 1000, "durationSeconds": 20}],
        maxVUs=50000,
    )
    rows = _rows(_calendar({"step-01": 200}, protocol), protocol)
    step = rows["step-01"]
    assert step["load"]["verdict"] == "not qualifiable: generator limit"
    assert step["sustainability"] == "not assessed"
    assert step["sent"] == step["completed"] == 200 * 20
    assert step["withinThresholdFraction"] == 1.0


def test_deviation_exactly_at_threshold_is_admitted_and_above_is_not():
    protocol = _protocol(
        status="frozen",
        generator=GENERATOR,
        steps=[{"rate": 20, "durationSeconds": 10}],
    )
    at = _rows(_calendar({"step-01": 19}, protocol), protocol)["step-01"]
    assert at["load"]["deviation"] == pytest.approx(0.05)
    assert at["load"]["verdict"] == "delivered"
    above = _rows(_calendar({"step-01": 18}, protocol), protocol)["step-01"]
    assert above["load"]["verdict"] != "delivered"


def test_dropped_iterations_alone_disqualify_a_step():
    protocol = _protocol(status="frozen", generator=GENERATOR)
    summary = {
        "metrics": {
            **NO_DROPS["metrics"],
            "dropped_iterations": {"count": 100},
            "dropped_iterations{scenario:step-01}": {"count": 100},
        }
    }
    rows = _rows(_calendar({}, protocol), protocol, summary)
    assert "dropped iterations above threshold" in rows["step-01"]["load"]["reasons"]
    assert rows["step-02"]["load"]["verdict"] == "delivered"


def test_missing_dropped_metric_is_not_read_as_zero():
    protocol = _protocol(status="frozen", generator=GENERATOR)
    summary = {"metrics": {"dropped_iterations": {"count": 3}}}
    rows = _rows(_calendar({}, protocol), protocol, summary)
    assert "dropped_iterations missing" in rows["step-01"]["load"]["reasons"]


def test_late_start_disqualifies_even_if_the_total_is_recovered():
    protocol = _protocol(
        status="frozen",
        generator=GENERATOR,
        steps=[{"rate": 20, "durationSeconds": 20}],
    )
    events = [e for e in _calendar({}, protocol) if e.get("phase") != "step-01"]
    start = next(p for p in protocol.phases() if p["name"] == "step-01")
    # Nothing for 5 s, then a catch-up that restores the step's total.
    for second in range(5, 20):
        sends = _send("step-01", start["startSeconds"] + second, 20 + 7)
        events += sends + _done(sends)
    step = _rows(events, protocol)["step-01"]
    assert abs(step["sent"] - 400) / 400 <= 0.05
    assert step["load"]["startDelaySeconds"] == pytest.approx(5)
    assert "window rate deviation above threshold" in step["load"]["reasons"]
    assert "start delay above threshold" in step["load"]["reasons"]


def test_late_response_stays_with_its_send_cohort_and_pending_is_counted():
    protocol = _protocol()
    events = [{"t": "origin", "ts": ORIGIN}]
    sends = _send("warmup", 60, 4)
    late = _done(sends[:2], dur=45_000)  # completes inside step-01
    events += sends + late
    rows = _rows(events, protocol)
    assert rows["warmup"]["sent"] == 4
    assert rows["warmup"]["completed"] == 2
    assert rows["warmup"]["pending"] == 2
    assert rows["warmup"]["withinThreshold"] == 0
    assert rows["warmup"]["withinThresholdFraction"] == 0.0
    assert rows["step-01"]["sent"] == 0


def test_pilot_steps_are_unverified_never_delivered():
    protocol = _protocol()
    rows = _rows(_calendar({}, protocol), protocol)
    assert rows["step-01"]["load"]["verdict"] == "unverified"
    assert rows["step-01"]["sustainability"] == "not assessed"


def test_ledger_flags_duplicates_and_orphans():
    sends = _send("warmup", 60, 1)
    ledger = build_ledger(
        [
            *sends,
            *sends,
            *_done(sends),
            *_done(sends),
            {"t": "done", "id": "ghost", "phase": "warmup", "ts": 1, "dur": 1},
        ]
    )
    kinds = Counter(anomaly.split()[0] for anomaly in ledger["anomalies"])
    assert kinds == {"duplicate": 1, "second": 1, "terminal": 1}


@pytest.mark.parametrize("request_id", ["warmup-1", 'warmup-"quoted\\path\nline'])
def test_truncated_and_logfmt_events_are_read_honestly(tmp_path, request_id):
    event = {"t": "send", "id": request_id, "phase": "warmup", "ts": 1.0}
    logfmt = (
        'time="x" level=info msg=' + json.dumps(json.dumps(event)) + " source=console"
    )
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(event) + "\n" + logfmt + "\n" + '{"t": "do')
    events, malformed = read_events(path)
    assert events == [event, event]
    assert malformed == 1


def test_timeouts_and_errors_are_distinct_terminals():
    protocol = _protocol()
    sends = _send("warmup", 60, 3)
    events = [
        {"t": "origin", "ts": ORIGIN},
        *sends,
        {
            "t": "timeout",
            "id": sends[0]["id"],
            "phase": "warmup",
            "ts": 1,
            "dur": 30000,
        },
        {"t": "error", "id": sends[1]["id"], "phase": "warmup", "ts": 1, "dur": 1},
        *_done(sends[2:], status=429, ok=False),
    ]
    row = _rows(events, protocol)["warmup"]
    assert (row["timeouts"], row["errors"], row["rejected"], row["pending"]) == (
        1,
        1,
        1,
        0,
    )


def test_resources_are_summarised_per_phase_with_idle_delta():
    protocol = _protocol()
    start = datetime.fromtimestamp(ORIGIN / 1000, UTC)

    def points(values):
        return [
            {
                "timestamp": datetime.fromtimestamp(ORIGIN / 1000 + t, UTC).isoformat(),
                "value": v,
            }
            for t, v in values
        ]

    snapshot = {
        "start": start.isoformat(),
        "queries": {
            "container_memory_bytes@control-plane": {
                "points": points([(10, 100.0), (70, 160.0), (130, 200.0)])
            },
            "container_cpu_cores_max@control-plane": {
                "points": points([(35, 0.1), (65, 0.9), (95, 0.5)])
            },
        },
    }
    rows = summarize_phases(
        protocol,
        build_ledger([{"t": "origin", "ts": ORIGIN}]),
        summary=NO_DROPS,
        snapshot=snapshot,
    )
    by = {row["phase"]: row for row in rows}
    assert by["idle"]["workingSetBytes"]["mean"] == 100.0
    assert by["warmup"]["workingSetDeltaFromIdle"] == 60.0
    # The CPU point at 65 s summarises 35-65 s, across the idle/warmup boundary.
    assert by["warmup"]["cpuCores"]["samples"] == 1
    assert by["warmup"]["cpuCores"]["mean"] == 0.5
    assert by["step-02"]["cpuCores"] == {"mean": None, "max": None, "samples": 0}


def test_missing_series_is_unavailable_not_zero():
    protocol = _protocol()
    rows = summarize_phases(
        protocol,
        build_ledger([{"t": "origin", "ts": ORIGIN}]),
        summary=NO_DROPS,
        snapshot={"queries": {}},
    )
    assert all(row["workingSetBytes"] is None for row in rows)


def test_report_lists_failed_and_incomplete_cells_without_inventing_phases(tmp_path):
    protocol = _protocol()
    cells = build_experiment_01_matrix(EXPERIMENT_01_VARIANTS, 1)[:2]
    selected = attempt_dir(cells[0].run_dir(tmp_path), 1)
    (selected / "diagnostics").mkdir(parents=True)
    (selected / "diagnostics/pods.json").write_text("{}")
    _receipt(tmp_path, cells[0], 1, "startup_failed")
    path = write_experiment_01_report(tmp_path, cells, protocol)
    summary = json.loads((tmp_path / "experiment-01-summary.json").read_text())
    assert [s["outcome"] for s in summary] == ["startup_failed", "incomplete"]
    assert all(s["phases"] == [] for s in summary)
    assert path.read_text().count("\n") == 3


# ---------------------------------------------------------------- generator


def test_k6_environment_carries_one_protocol_and_the_exact_payload():
    from nanolab.config.scenario import ScenarioConfig

    scenario = ScenarioConfig.model_validate(
        yaml.safe_load(
            (TOOL_ROOT / "scenarios" / "experiment-01-pilot.yaml").read_text()
        )
    )
    payload = b'{"size":1024,"text":"A"}'
    environment = experiment_01_k6_environment(scenario, payload)
    protocol = json.loads(environment["E01_PROTOCOL"])
    assert environment["E01_PAYLOAD"] == '{"input":{"size":1024,"text":"A"}}'
    assert json.loads(environment["E01_PAYLOAD"]) == {"input": json.loads(payload)}
    assert environment["NANOFAAS_FUNCTION"] == "qr-code-rust"
    assert "NANOFAAS_NEIGHBOUR" not in environment
    assert protocol["gracefulStopSeconds"] == 40
    assert protocol["size"] == 1024
    assert protocol["phases"][0]["name"] == "idle"
    assert script_for(scenario) == EXPERIMENT_01_SCRIPT_NAME


# ---------------------------------------------------------------- manifest


def _git(root, *args):
    import subprocess

    return subprocess.check_output(("git", *args), cwd=root)


@pytest.fixture
def experiment_inputs(tmp_path):
    from nanolab.comparison.experiment_01 import experiment_01_identity
    from nanolab.comparison.manifest import capture_comparison_inputs
    from nanolab.config.environment import EnvironmentConfig, RoleTarget
    from nanolab.config.experiment_01 import experiment_01_payload
    from nanolab.config.scenario import ScenarioConfig
    from nanolab.workspace.paths import bundled_assets_root

    roots = {}
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
                "name: qr-code-rust\ncatalog:\n  defaultImage: test/qr:latest\n"
            )
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "initial")
        roots[name] = root
    scenario = ScenarioConfig.model_validate(
        yaml.safe_load(
            (TOOL_ROOT / "scenarios" / "experiment-01-pilot.yaml").read_text()
        )
    )
    protocol = scenario.experiment_01
    assert protocol is not None
    selected = JVM_KEYS
    cells = build_experiment_01_matrix(resolve_variants(selected), 3)

    def capture(**changes):
        kwargs = {
            "scenario": scenario,
            "environment": EnvironmentConfig(
                provider="multipass",
                roles={"stack": RoleTarget(name="s"), "loadgen": RoleTarget(name="l")},
            ),
            "nanofaas_root": roots["nanofaas"],
            "nanolab_root": roots["nanolab"],
            "profiles": experiment_01_profiles(TOOL_ROOT, selected),
            "variants": selected,
            "repetitions": 3,
            "tag": "recipe-test",
            "build_memory": None,
            "parallelism": None,
            "experiment": experiment_01_identity(
                protocol,
                cells,
                payload=experiment_01_payload(
                    protocol, TOOL_ROOT / "scenarios/payloads"
                ),
                script=bundled_assets_root() / "k6" / EXPERIMENT_01_SCRIPT_NAME,
                selected=selected,
            ),
            "profile_options": EXPERIMENT_01_CONTRACT.options,
            **changes,
        }
        return capture_comparison_inputs(**kwargs)

    return capture, cells


def test_experiment_identity_records_order_protocol_and_builds(experiment_inputs):
    capture, cells = experiment_inputs
    inputs = capture()
    experiment = inputs["experiment"]
    assert experiment["order"] == [cell.label for cell in cells]
    assert experiment["orderPolicy"] == "experiment-01-three-orders-v1"
    assert experiment["subset"] is True
    assert inputs["scheduler"]["strategy"] is None
    assert inputs["profiles"][JVM_KEYS[0]]["options"]["runtime"] == "tiered"


def test_payload_identity_hashes_the_sent_body(experiment_inputs):
    capture, _ = experiment_inputs
    payload = capture()["experiment"]["payload"]
    raw = (TOOL_ROOT / "scenarios/payloads/experiment-01-qr-1024.json").read_bytes()
    body = b'{"input":{"size":1024,"text":"' + b"A" * 1024 + b'"}}'
    assert payload["bodySha256"] == hashlib.sha256(body).hexdigest()
    assert payload["sha256"] == hashlib.sha256(raw).hexdigest()
    assert payload["bytes"] == len(raw)
    assert payload["sha256"] != payload["bodySha256"]


@pytest.mark.parametrize("value", [None, "invalid", "A" * 64])
def test_resume_refuses_missing_or_invalid_body_hash(experiment_inputs, value):
    from nanolab.comparison.manifest import (
        new_comparison_manifest,
        require_matching_inputs,
    )

    capture, cells = experiment_inputs
    inputs = capture()
    manifest = new_comparison_manifest(inputs, cells)
    changed = json.loads(json.dumps(inputs))
    payload = changed["experiment"]["payload"]
    if value is None:
        payload.pop("bodySha256", None)
    else:
        payload["bodySha256"] = value
    with pytest.raises(ValueError, match="identity"):
        require_matching_inputs(manifest, changed)


def test_resume_refuses_legacy_body_identity(tmp_path, experiment_inputs):
    from nanolab.comparison.manifest import (
        new_comparison_manifest,
        require_matching_inputs,
    )

    capture, cells = experiment_inputs
    inputs = capture()
    # A legacy manifest differs only in the absence of the sent-body identity.
    legacy = json.loads(json.dumps(inputs))
    legacy["experiment"]["payload"].pop("bodySha256", None)
    old_manifest = new_comparison_manifest(inputs, cells).model_copy(
        update={"identity": legacy}
    )
    with pytest.raises(ValueError, match=r"identity|differ"):
        require_matching_inputs(old_manifest, inputs)
    cell = cells[0]
    _measured_attempt(tmp_path, cell)
    write_cell_result(
        cell,
        tmp_path,
        identity=legacy,
        attempt=1,
        outcome="measured",
        reason="legacy",
        started_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="belong"):
        read_cell_result(cell, tmp_path, inputs)
    require_matching_inputs(new_comparison_manifest(inputs, cells), capture())


def test_experiment_resume_refuses_any_determining_change(tmp_path, experiment_inputs):
    from nanolab.comparison.manifest import (
        new_comparison_manifest,
        require_matching_inputs,
    )

    capture, cells = experiment_inputs
    inputs = capture()
    manifest = new_comparison_manifest(inputs, cells)
    require_matching_inputs(manifest, capture())
    changed = json.loads(json.dumps(inputs))
    changed["experiment"]["order"] = changed["experiment"]["order"][::-1]
    with pytest.raises(ValueError, match="differ"):
        require_matching_inputs(manifest, changed)
    incomplete = json.loads(json.dumps(inputs))
    del incomplete["experiment"]["payload"]
    with pytest.raises(ValueError, match="experiment identity"):
        require_matching_inputs(manifest, incomplete)


def test_historical_scheduler_identity_cannot_be_dropped(experiment_inputs):
    from nanolab.comparison.manifest import _Identity

    capture, _cells = experiment_inputs
    historical = capture()
    del historical["experiment"]
    with pytest.raises(ValueError, match="scheduler"):
        _Identity.model_validate(historical)


# ---------------------------------------------------------------- cell runner


class _Result:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.return_code = 0


class _Provider:
    def __init__(self, listings: list[dict]) -> None:
        self.listings = listings

    def exec_argv(self, request, argv, **_kwargs):
        return _Result(json.dumps(self.listings.pop(0)))


RUNNING = _pods(ready=True, state={"running": {}})
OOM = _pods(lastState={"terminated": {"reason": "OOMKilled", "exitCode": 137}})


def _run(tmp_path, attempts, listings):
    from nanolab.cli.comparison import _run_experiment_cell

    calls = []

    def run_attempt(run_dir: Path) -> None:
        behaviour = attempts[len(calls)]
        calls.append(run_dir)
        behaviour(run_dir)

    outcome = _run_experiment_cell(
        _cell(),
        root=tmp_path,
        identity=IDENTITY,
        provider=_Provider(listings),  # type: ignore[arg-type]
        request=None,  # type: ignore[arg-type]
        before_attempt=lambda: None,
        run_attempt=run_attempt,
    )
    return outcome, calls


def _fail(run_dir: Path) -> None:
    raise RuntimeError("boom")


def _fail_after_load(run_dir: Path) -> None:
    from nanolab.comparison.experiment_01 import mark_load_started

    mark_load_started(run_dir)
    raise RuntimeError("k6 died")


def _measure(run_dir: Path) -> None:
    (run_dir / "metrics").mkdir()
    (run_dir / "k6-summary.json").write_text("{}")
    (run_dir / "k6-events.jsonl").write_text("")
    (run_dir / "metrics/prometheus-snapshot.json").write_text("{}")


def test_oom_is_committed_without_retry(tmp_path):
    outcome, calls = _run(tmp_path, [_fail], [OOM])
    assert outcome == "oom" and len(calls) == 1
    receipt = json.loads((_cell().run_dir(tmp_path) / CELL_RESULT).read_text())
    assert receipt["outcome"] == "oom"
    assert "diagnostics/pods.json" in receipt["evidence"]


def test_transport_failure_before_load_is_retried_once_in_a_new_attempt(tmp_path):
    outcome, calls = _run(tmp_path, [_fail, _measure], [RUNNING, RUNNING])
    assert outcome == "measured"
    assert [path.name for path in calls] == ["1", "2"]
    receipt = json.loads((_cell().run_dir(tmp_path) / CELL_RESULT).read_text())
    assert receipt["attempt"] == 2


def test_second_transport_failure_stops(tmp_path):
    with pytest.raises(RuntimeError, match="boom"):
        _run(tmp_path, [_fail, _fail], [RUNNING, RUNNING])
    assert not (_cell().run_dir(tmp_path) / CELL_RESULT).exists()


def test_error_after_load_started_stops_with_partial_evidence(tmp_path):
    with pytest.raises(RuntimeError, match="k6 died"):
        _run(tmp_path, [_fail_after_load], [RUNNING])
    cell_dir = _cell().run_dir(tmp_path)
    assert not (cell_dir / CELL_RESULT).exists()
    assert (attempt_dir(cell_dir, 1) / "diagnostics/error.txt").is_file()


def test_failed_attempt_records_remote_evidence_recovery_notes(tmp_path):
    from nanolab.comparison.experiment_01 import mark_load_started

    def fail_with_recovery_note(run_dir):
        mark_load_started(run_dir)
        error = RuntimeError("k6 died")
        error.add_note("Could not fetch k6 events from /tmp/attempt-1: SSH failed")
        raise error

    with pytest.raises(RuntimeError, match="k6 died"):
        _run(tmp_path, [fail_with_recovery_note], [RUNNING])
    diagnostic = attempt_dir(_cell().run_dir(tmp_path), 1) / "diagnostics/error.txt"
    assert diagnostic.read_text() == (
        "RuntimeError: k6 died\n"
        "Could not fetch k6 events from /tmp/attempt-1: SSH failed\n"
    )


def test_failed_fresh_attempt_stays_pending_and_preserves_previous_receipt(tmp_path):
    _run(tmp_path, [_measure], [RUNNING])
    cell = _cell()
    cell_dir = cell.run_dir(tmp_path)
    previous = (cell_dir / CELL_RESULT).read_bytes()
    with pytest.raises(RuntimeError, match="k6 died"):
        _run(tmp_path, [_fail_after_load], [RUNNING])
    assert pending((cell,), tmp_path, experiment_completed(IDENTITY)) == (cell,)
    assert (attempt_dir(cell_dir, 1) / CELL_RESULT).read_bytes() == previous
    assert (attempt_dir(cell_dir, 1) / "k6-summary.json").read_text() == "{}"
    assert (attempt_dir(cell_dir, 2) / "diagnostics/error.txt").is_file()
    _run(tmp_path, [_measure], [RUNNING])
    receipt = read_cell_result(cell, tmp_path, IDENTITY)
    assert receipt is not None
    assert receipt["attempt"] == 3
    assert (attempt_dir(cell_dir, 1) / CELL_RESULT).read_bytes() == previous


def test_fresh_attempt_is_pending_even_if_preparation_fails(tmp_path):
    from nanolab.cli.comparison import _run_experiment_cell

    _run(tmp_path, [_measure], [RUNNING])
    cell = _cell()

    def unavailable() -> None:
        raise RuntimeError("target unavailable")

    with pytest.raises(RuntimeError, match="target unavailable"):
        _run_experiment_cell(
            cell,
            root=tmp_path,
            identity=IDENTITY,
            provider=_Provider([]),  # type: ignore[arg-type]
            request=None,  # type: ignore[arg-type]
            before_attempt=unavailable,
            run_attempt=_measure,
        )
    assert pending((cell,), tmp_path, experiment_completed(IDENTITY)) == (cell,)
    assert (attempt_dir(cell.run_dir(tmp_path), 1) / CELL_RESULT).is_file()


def test_oom_during_a_completed_load_is_an_oom(tmp_path):
    outcome, _calls = _run(tmp_path, [_measure], [OOM])
    assert outcome == "oom"


def test_host_facts_are_read_per_role_and_malformed_ones_refused():
    from nanolab.comparison.target import read_host_facts

    class Provider:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

        def exec_argv(self, request, argv, **_kwargs):
            return _Result(self.stdout)

    good = "x86_64\n6.8.0\n8\n32000000\nUbuntu 24.04 LTS\n"
    facts = read_host_facts(Provider(good), {"stack": None, "loadgen": None})  # type: ignore[arg-type,dict-item]
    assert facts["stack"] == {
        "architecture": "x86_64",
        "kernel": "6.8.0",
        "cpus": 8,
        "memoryKiB": 32000000,
        "os": "Ubuntu 24.04 LTS",
    }
    with pytest.raises(ValueError, match="host facts"):
        read_host_facts(Provider("x86_64\n"), {"stack": None})  # type: ignore[arg-type,dict-item]
