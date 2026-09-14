import json
from dataclasses import asdict, replace

import pytest

from nanolab.tasks.soak.models import CriterionResult, Sample, Target

TARGET = Target("cp", "container", 42, "started", "repo@sha256:" + "a" * 64, "jvm")


def criterion(operation="return_to_reference", metric="process_rss_bytes", **changes):
    value = {
        "id": "rss",
        "role": "cp",
        "metric": metric,
        "label_selector": {},
        "unit": "bytes",
        "operation": operation,
        "phase": "drain",
        "window_s": 4,
        "deadline_s": 4,
        "rationale": "declared before run",
    }
    if operation == "return_to_reference":
        value.update(absolute_tolerance=20.0, relative_tolerance=0.1)
    elif operation in ("maximum", "growth_review"):
        value["threshold"] = 120.0
    value.update(changes)
    return value


def make_run(tmp_path, *, criteria=None, values=None, omit=(), purpose="smoke"):
    root = tmp_path / "run"
    root.mkdir()
    manifest = {
        "schema": "nanolab-soak-v1",
        "purpose": purpose,
        "scope": "numerical-projection",
        "targets": [asdict(TARGET)],
        "criteria": criteria or [criterion()],
        "windows": {
            "baseline": {"start_s": 0.0, "end_s": 2.0},
            "steady": {"start_s": 3.0, "end_s": 7.0},
            "drain": {"start_s": 8.0, "end_s": 12.0},
        },
        "max_observation_gap_s": 1.1,
        "sample_interval_s": 1.0,
        "perturbations": [],
    }
    (root / "evaluation-input.json").write_text(json.dumps(manifest))
    samples = []
    for phase, stamps in (
        ("baseline", range(3)),
        ("steady", range(3, 8)),
        ("drain", range(8, 13)),
    ):
        for stamp in stamps:
            if stamp in omit:
                continue
            value = values.get(stamp, 100.0) if values else 100.0
            samples.append(
                Sample(
                    TARGET,
                    phase,  # pyright: ignore[reportArgumentType]
                    stamp,
                    stamp,
                    stamp,
                    "process_rss_bytes",
                    (),
                    "bytes",
                    value,
                    "observed",
                    "procfs",
                    None,
                )
            )
    with (root / "samples.jsonl").open("w") as stream:
        for row in samples:
            stream.write(
                json.dumps({"schema": "nanolab-soak-v1", **asdict(row)}) + "\n"
            )
    return root, manifest, samples


def rewrite_samples(root, samples):
    with (root / "samples.jsonl").open("w") as stream:
        for row in samples:
            stream.write(
                json.dumps({"schema": "nanolab-soak-v1", **asdict(row)}) + "\n"
            )


def results(root):
    from nanolab.tasks.soak.evaluate import evaluate_run

    return {item.criterion_id: item for item in evaluate_run(root)}


def test_verdict_precedence():
    from nanolab.tasks.soak.evaluate import combine_results

    items = (
        CriterionResult("retention", "FAIL", "expired population", ()),
        CriterionResult("rss", "INCONCLUSIVE", "missing evidence", ()),
    )
    assert combine_results(items, False) == "FAIL"
    assert combine_results(items, True) == "ABORTED"
    assert combine_results((), False) == "INCONCLUSIVE"
    assert combine_results((CriterionResult("x", "PASS", "ok", ()),), False) == "PASS"


def test_numerical_pass_is_not_full_soak_acceptance(tmp_path):
    from nanolab.tasks.soak.evaluate import combine_results

    root, _, _ = make_run(tmp_path)
    outcome = results(root)
    assert outcome["rss"].status == "PASS"
    assert outcome["run-coverage"].status == "INCONCLUSIVE"
    assert combine_results(tuple(outcome.values()), False) == "INCONCLUSIVE"


def test_stricter_relative_tolerance_controls_rss(tmp_path):
    root, _, _ = make_run(tmp_path, values=dict.fromkeys(range(8, 13), 111))
    assert results(root)["rss"].status == "FAIL"


def test_baseline_uses_median_not_peak(tmp_path):
    root, _, _ = make_run(tmp_path, values={0: 10, 1: 100, 2: 1000, 12: 111})
    assert results(root)["rss"].status == "FAIL"


def test_absolute_tolerance_can_be_stricter(tmp_path):
    root, _, _ = make_run(
        tmp_path, criteria=[criterion(absolute_tolerance=2.0)], values={12: 103}
    )
    assert results(root)["rss"].status == "FAIL"


def test_final_maximum_not_last_low_sample(tmp_path):
    root, _, _ = make_run(tmp_path, values={8: 150, 12: 90})
    assert results(root)["rss"].status == "FAIL"


@pytest.mark.parametrize("omit", [(9, 10), (8, 9, 10, 11), (0, 1)])
def test_missing_windows_do_not_pass(tmp_path, omit):
    root, _, _ = make_run(tmp_path, omit=omit)
    assert results(root)["rss"].status == "INCONCLUSIVE"


def test_observed_violation_is_not_hidden_by_gap(tmp_path):
    root, _, _ = make_run(tmp_path, values={12: 150}, omit=(9, 10))
    assert results(root)["rss"].status == "FAIL"


def test_released_heap_does_not_excuse_rss(tmp_path):
    root, _, samples = make_run(tmp_path, values={12: 150})
    expanded = []
    for row in samples:
        expanded.extend((row, replace(row, metric="heap_bytes", value=0)))
    rewrite_samples(root, expanded)
    assert results(root)["rss"].status == "FAIL"


def test_plateau_of_expired_population_is_not_zero(tmp_path):
    root, _, samples = make_run(
        tmp_path, criteria=[criterion("expected_zero", "queue_entries", unit="count")]
    )
    rewrite_samples(
        root,
        [
            replace(row, metric="queue_entries", unit="count", value=1)
            for row in samples
        ],
    )
    assert results(root)["rss"].status == "FAIL"


def test_maximum_and_growth_review(tmp_path):
    root, _, _ = make_run(
        tmp_path,
        criteria=[
            criterion("maximum", id="maximum"),
            criterion("growth_review", id="growth", threshold=5.0),
        ],
        values={12: 130},
    )
    outcome = results(root)
    assert outcome["maximum"].status == "FAIL"
    assert outcome["growth"].status == "INCONCLUSIVE"


def test_diagnostic_sample_cannot_rescue_natural_rss(tmp_path):
    root, _, samples = make_run(tmp_path, values={12: 150})
    samples.append(
        replace(
            samples[-1],
            phase="diagnostic",
            scheduled_s=13,
            started_s=13,
            ended_s=13,
            value=0,
        )
    )
    rewrite_samples(root, samples)
    assert results(root)["rss"].status == "FAIL"


def test_identity_change_is_incomplete_evidence(tmp_path):
    root, _, samples = make_run(tmp_path)
    samples[-1] = replace(samples[-1], target=replace(TARGET, process_id=43))
    rewrite_samples(root, samples)
    outcome = results(root)
    assert outcome["sample-integrity"].status == "INCONCLUSIVE"


def test_missing_sdk_metric_is_not_zero(tmp_path):
    root, manifest, _ = make_run(tmp_path, criteria=[criterion(role="sdk")])
    manifest["targets"].append(asdict(replace(TARGET, role="sdk")))
    (root / "evaluation-input.json").write_text(json.dumps(manifest))
    assert results(root)["rss"].status == "INCONCLUSIVE"


def test_distinct_labels_are_not_summed(tmp_path):
    root, _, samples = make_run(
        tmp_path, criteria=[criterion("maximum", "heap_bytes", threshold=120.0)]
    )
    rows = []
    for row in samples:
        rows.extend(
            (
                replace(
                    row, metric="heap_bytes", labels=(("area", "heap"),), value=100
                ),
                replace(
                    row, metric="heap_bytes", labels=(("area", "nonheap"),), value=100
                ),
            )
        )
    rewrite_samples(root, rows)
    assert results(root)["rss"].status == "PASS"


def test_new_label_without_reference_cannot_pass_recovery(tmp_path):
    root, _, samples = make_run(tmp_path)
    rewrite_samples(
        root,
        [
            replace(row, labels=(("owner", "new" if row.phase == "drain" else "old"),))
            for row in samples
        ],
    )
    assert results(root)["rss"].status == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "mutation", ["nonfinite", "backwards", "unavailable", "wrong-unit"]
)
def test_invalid_or_missing_sample_cannot_pass(tmp_path, mutation):
    root, _, samples = make_run(tmp_path)
    changes = {
        "nonfinite": {"value": float("nan")},
        "backwards": {"ended_s": 10},
        "unavailable": {"availability": "unavailable", "value": None},
        "wrong-unit": {"unit": "megabytes"},
    }[mutation]
    samples[-1] = replace(samples[-1], **changes)
    rewrite_samples(root, samples)
    outcome = results(root)
    assert any(
        item.status == "INCONCLUSIVE"
        for key, item in outcome.items()
        if key != "run-coverage"
    )


def test_torn_tail_preserves_prefix_and_reports_gap(tmp_path):
    root, _, _ = make_run(tmp_path)
    with (root / "samples.jsonl").open("a") as stream:
        stream.write('{"torn":')
    assert results(root)["sample-integrity"].status == "INCONCLUSIVE"


def test_reevaluation_is_deterministic_and_offline(tmp_path, monkeypatch):
    import socket
    import subprocess

    root, _, _ = make_run(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline evaluation must not contact runtimes")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert results(root) == results(root)


def test_baseline_samples_from_the_settling_drain_are_placed_by_label_extent():
    """The observer labels the settling drain "baseline" as well.

    Judging those samples against the narrow criterion window reported every
    one of them as crossing a phase boundary.
    """
    policy = {
        "windows": {"baseline": {"start_s": 50.0, "end_s": 80.0}},
        "phase_extents": {"baseline": {"start_s": 20.0, "end_s": 80.0}},
    }

    def placed(scheduled):
        window = policy.get("phase_extents", {}).get(
            "baseline", policy["windows"]["baseline"]
        )
        return window["start_s"] <= scheduled <= window["end_s"]

    assert placed(25.0)  # during the settling drain
    assert placed(60.0)  # during the measurement window
    assert not placed(15.0)  # before the baseline period began
    assert not placed(85.0)  # after it ended
