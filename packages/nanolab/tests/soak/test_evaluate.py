import json
import math
import re
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


def _tick(samples, phase, at):
    """Build a sample shaped like the observer's, at one scheduled instant."""
    return replace(samples[0], phase=phase, scheduled_s=at, started_s=at, ended_s=at)


BUCKET_METRIC = "scheduler_switch_duration_seconds_bucket"
# A bucket family is cumulative, so the counts a window reads are what it grew
# by across the window. These two shapes are deliberately far apart: the level
# the family had already reached before the window opened is all in the slowest
# bucket, while the window's own thousand observations are almost all fast. The
# p99 of the window's own count is 0.1167 s; the p99 of the two shapes added
# together is 0.2489 s, so a criterion that reads the wrong one answers with a
# number that is a reading of something else rather than with an error.
_ALREADY_THERE = {"0.05": 0, "0.1": 0, "0.15": 0, "0.25": 10000, "+Inf": 10000}
_THIS_WINDOW = {"0.05": 900, "0.1": 985, "0.15": 1000, "0.25": 1000, "+Inf": 1000}


def percentile_criterion(**changes):
    value = {
        "id": "p99",
        "role": "cp",
        "metric": BUCKET_METRIC,
        "label_selector": {},
        "unit": "seconds",
        "operation": "percentile",
        "quantile": 0.99,
        "phase": "steady",
        "window_s": 4,
        "threshold": 0.12,
        "rationale": "declared before run",
    }
    value.update(changes)
    return value


def bucket_rows(by_stamp):
    """One family per scrape, in time order, as the observer writes them."""
    return [
        Sample(
            TARGET,
            "steady",
            stamp,
            stamp,
            stamp,
            BUCKET_METRIC,
            (("le", bound),),
            "seconds",
            value,
            "observed",
            "prometheus",
            None,
        )
        for stamp, counts in by_stamp.items()
        for bound, value in counts.items()
    ]


def bucket_scrapes(observed=None):
    """Give the family's level at each scrape of the steady window."""
    observed = _THIS_WINDOW if observed is None else observed
    family = {bound: _ALREADY_THERE[bound] + grown for bound, grown in observed.items()}
    # Flat until the last scrape, which is what a family that only grows when a
    # switch is made actually looks like between two of them.
    by_stamp: dict[int, dict[str, int]] = dict.fromkeys((3, 4, 5, 6), _ALREADY_THERE)
    by_stamp[7] = family
    return by_stamp


def make_bucket_run(tmp_path, *, observed=None, **changes):
    """Build a run whose steady window carried one bucket family per scrape."""
    root, _, _ = make_run(tmp_path, criteria=[percentile_criterion(**changes)])
    rewrite_samples(root, bucket_rows(bucket_scrapes(observed)))
    return root


def test_bucket_quantile_interpolates_inside_the_containing_bucket():
    """The rank's position inside its bucket is linear in the counts.

    The hand-computed answer: rank = 0.99 * 1000 = 990, the first bucket that
    reaches it is `le=0.15` at 1000, so the value is 0.15 + (0.25 - 0.15) *
    (990 - 600) / (1000 - 600) = 0.2475. Reading the bucket's own upper bound,
    or the last finite bound, answers 0.25 instead.
    """
    from nanolab.tasks.soak.evaluate import bucket_quantile

    buckets = {0.05: 0.0, 0.1: 100.0, 0.15: 600.0, 0.25: 1000.0, math.inf: 1000.0}
    assert bucket_quantile(0.99, buckets) == pytest.approx(0.2475)


def test_bucket_quantile_answers_the_largest_finite_bound_above_it():
    """A rank inside the open-ended bucket has no bound to interpolate to.

    Prometheus answers with the largest finite bound rather than with infinity,
    which is also the only answer that cannot understate the reading.
    """
    from nanolab.tasks.soak.evaluate import bucket_quantile

    buckets = {0.05: 0.0, 0.1: 100.0, 0.25: 200.0, math.inf: 1000.0}
    assert bucket_quantile(0.99, buckets) == 0.25


@pytest.mark.parametrize(
    "reason",
    ["no +Inf bound", "only an open-ended bucket", "not cumulative", "no observation"],
)
def test_bucket_quantile_refuses_a_family_it_cannot_place(reason):
    """Every way of not answering raises.

    Returning a number instead is what turns an absent series into a comfortable
    zero downstream.
    """
    from nanolab.tasks.soak.evaluate import bucket_quantile

    buckets = {
        "no +Inf bound": {0.05: 0.0, 0.1: 5.0},
        "only an open-ended bucket": {math.inf: 5.0},
        "not cumulative": {0.05: 40.0, 0.1: 5.0, math.inf: 5.0},
        "no observation": {0.05: 0.0, 0.1: 0.0, math.inf: 0.0},
    }[reason]
    with pytest.raises(ValueError, match=re.escape(reason)):
        bucket_quantile(0.99, buckets)


def test_the_percentile_is_read_from_the_window_not_the_process_lifetime(tmp_path):
    """The window's own reading of a cumulative family, and interpolated.

    Threshold 0.12 against a true window p99 of 0.1167 s. A criterion that read
    the family's level instead of its growth, or that returned the containing
    bucket's upper bound, both answer above the threshold and fail here.
    """
    root = make_bucket_run(tmp_path)
    assert results(root)["p99"].status == "PASS"


def test_a_percentile_over_the_budget_fails_the_run(tmp_path):
    root = make_bucket_run(tmp_path, threshold=0.1)
    outcome = results(root)["p99"]
    assert outcome.status == "FAIL"
    assert "p99=" in outcome.reason


def test_an_absent_bucket_family_is_not_a_percentile_of_zero(tmp_path):
    """The hazard this operation exists against, in its plainest form.

    Nothing published the family, so there is nothing to derive a quantile from.
    A criterion that answered zero would make a platform with no histogram look
    like a platform whose pauses were instantaneous.
    """
    root = make_bucket_run(tmp_path, metric="scheduler_switch_duration_seconds_p99")
    outcome = results(root)["p99"]
    assert outcome.status == "INCONCLUSIVE"
    assert "series absent" in outcome.reason


def test_a_bucket_family_of_zeros_is_not_a_percentile_of_zero(tmp_path):
    """A family that answered, with nothing in it, is still not a reading."""
    root = make_bucket_run(tmp_path, observed=dict.fromkeys(_THIS_WINDOW, 0))
    outcome = results(root)["p99"]
    assert outcome.status == "INCONCLUSIVE"
    assert "no observation" in outcome.reason


def test_a_metric_without_le_bounds_is_not_a_bucket_family(tmp_path):
    """Pointing the operation at a plain series is refused, not answered."""
    root, _, samples = make_run(tmp_path, criteria=[percentile_criterion()])
    rewrite_samples(
        root,
        [
            replace(row, metric=BUCKET_METRIC, unit="seconds", value=1.0)
            for row in samples
        ],
    )
    outcome = results(root)["p99"]
    assert outcome.status == "INCONCLUSIVE"
    assert "`le` bound" in outcome.reason


def test_a_bucket_count_that_fell_inside_the_window_is_refused(tmp_path):
    """A reset in the window is not a window that observed nothing.

    The window's reading is the family's growth from its first sample, so a
    control-plane restart inside the window leaves it subtracting one process's
    counter from another's. Here the pre-reset level is read as the floor and the
    post-reset counts never reach it, which made every growth zero: reported as
    "no observation fell in the window", about a window that observed ten
    thousand switches, and the one reason that sends an operator looking for a
    series that is right there.
    """
    root, _, _ = make_run(tmp_path, criteria=[percentile_criterion()])
    before = dict.fromkeys(_ALREADY_THERE, 10000)
    after = {"0.05": 900, "0.1": 985, "0.15": 1000, "0.25": 1000, "+Inf": 1000}
    rewrite_samples(
        root, bucket_rows({3: before, 4: after, 5: after, 6: after, 7: after})
    )

    outcome = results(root)["p99"]
    assert outcome.status == "INCONCLUSIVE"
    assert "fell inside the window" in outcome.reason
    assert "no observation" not in outcome.reason


def test_a_scrape_that_missed_a_metric_is_named_as_a_tick_not_a_shape(tmp_path):
    """The unlabelled row a failed scrape leaves behind is not the family's shape.

    A scrape that did not carry a required metric is written as one unlabelled
    `unavailable` row, and that label set reached the operation as a series
    without an `le` — answered as "not a bucket family", which discarded the good
    samples every real family had contributed and pointed the operator at the
    exposition instead of at the scrape.
    """
    root = make_bucket_run(tmp_path)
    tick = Sample(
        TARGET,
        "steady",
        4.0,
        4.0,
        4.0,
        BUCKET_METRIC,
        (),
        "seconds",
        None,
        "unavailable",
        "exposition",
        "required metric absent from source",
    )
    rewrite_samples(
        root,
        sorted(
            [*bucket_rows(bucket_scrapes()), tick],
            key=lambda row: row.scheduled_s,
        ),
    )

    outcome = results(root)["p99"]
    assert outcome.status == "INCONCLUSIVE"
    assert "not a bucket family" not in outcome.reason
    # The family that did answer still contributed its reading: the p99 is what
    # this criterion is for, and the tick is what is missing.
    assert "labels=[]: 0.116667" in outcome.reason


def test_a_window_that_lost_an_observation_does_not_pass(tmp_path):
    """A gapped window is INCONCLUSIVE, not a pass on what it did read.

    The value is still the window's own and still under the threshold, so the
    operation has a number to answer with — and the one verdict it may not give
    on a window the run has already lost samples of is a pass. The acceptance
    gate re-checks the same evidence, so this cannot flip a run; what it decides
    is whether the criterion says "PASS" about a window it did not observe whole.
    """
    root, _, _ = make_run(tmp_path, criteria=[percentile_criterion()])
    rewrite_samples(
        root,
        [
            replace(
                row,
                value=None,
                availability="unavailable",
                reason="source failed on this tick",
            )
            if row.scheduled_s == 4.0 and ("le", "0.05") in row.labels
            else row
            for row in bucket_rows(bucket_scrapes())
        ],
    )

    outcome = results(root)["p99"]
    assert "p99=0.116667" in outcome.reason  # the number it would have answered
    assert outcome.status == "INCONCLUSIVE"
    assert "missing final samples" in outcome.reason


def test_the_worst_bucket_family_decides_the_criterion(tmp_path):
    """One family over budget is not excused by another under it.

    The rows are written in time order, as the observer writes them: the
    evaluator's stream cursor is monotone per role, so a file grouped by series
    instead of by tick reads as a torn timeline rather than as evidence.
    """
    root, _, _ = make_run(tmp_path, criteria=[percentile_criterion()])
    fast = {"0.05": 900, "0.1": 985, "0.15": 1000, "0.25": 1000, "+Inf": 1000}
    slow = {"0.05": 0, "0.1": 100, "0.15": 900, "0.25": 1000, "+Inf": 1000}
    rows = [
        Sample(
            TARGET,
            "steady",
            stamp,
            stamp,
            stamp,
            BUCKET_METRIC,
            (("le", bound), ("strategy", strategy)),
            "seconds",
            _ALREADY_THERE[bound] + (grown[bound] if stamp == 7 else 0),
            "observed",
            "prometheus",
            None,
        )
        for stamp in (3, 4, 5, 6, 7)
        for strategy, grown in (("per-function", fast), ("shared-queue", slow))
        for bound in sorted(_ALREADY_THERE)
    ]
    rewrite_samples(root, rows)
    outcome = results(root)["p99"]
    assert outcome.status == "FAIL"
    assert "shared-queue" in outcome.reason


def test_a_tick_stamped_after_its_phase_closed_cannot_pass(tmp_path):
    """The observer keeps its last label while diagnostics are captured.

    `workflow._natural` is the only thing that relabels the running observer and
    the last call is the drain phase, so every tick during `final_capture` is
    written with the drain label and a timestamp past the drain window's end.
    `_index` judges those against the drain extent, which is exactly that window,
    and reports a boundary crossing -- so a run passes or fails by whether a tick
    happens to land inside a capture that takes far longer than one interval.
    """
    root, _, samples = make_run(tmp_path)
    rewrite_samples(root, [*samples, _tick(samples, "drain", 13.0)])

    integrity = results(root)["sample-integrity"]
    assert integrity.status == "INCONCLUSIVE"
    assert "sample crosses natural phase boundary" in integrity.reason


def test_a_diagnostic_tick_does_not_break_the_natural_timeline(tmp_path):
    """A capture between two natural phases must not read as non-monotone.

    The planned baseline capture runs after the baseline window closes and before
    steady begins. Labeled `diagnostic` it is ranked above `steady`, so a tick
    carrying it arrives out of order against the steady ticks that follow --
    unless the diagnostic label is exempt from the comparison and does not
    advance the per-role cursor.
    """
    root, _, samples = make_run(tmp_path)
    rewrite_samples(root, [*samples, _tick(samples, "diagnostic", 2.5)])

    integrity = results(root)["sample-integrity"]
    assert integrity.status == "PASS"
    assert integrity.reason is None or "non-monotone" not in integrity.reason
