"""Offline numerical evaluation with a disk-backed sample/label index.

evaluation-input.json is a derived numerical projection, not an alternative run
policy. Full-run acceptance additionally requires bound receipts and frozen policy.
"""

import json
import math
import sqlite3
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TypeGuard

from nanolab.config.soak import Criterion
from nanolab.tasks.soak.acceptance import (
    evaluate_acceptance,
    required_sample_count,
)
from nanolab.tasks.soak.artifacts import read_records
from nanolab.tasks.soak.models import CriterionResult, Status, Target


def combine_results(results: tuple[CriterionResult, ...], aborted: bool) -> Status:
    """Preserve abort/failure precedence without treating absent evidence as PASS."""
    if aborted or any(item.status == "ABORTED" for item in results):
        return "ABORTED"
    if any(item.status == "FAIL" for item in results):
        return "FAIL"
    if not results or any(item.status != "PASS" for item in results):
        return "INCONCLUSIVE"
    return "PASS"


def _number(value: object) -> TypeGuard[float]:
    try:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
        )
    except OverflowError:
        return False


def _document(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("symlink evaluation inputs are unsupported")
    with path.open("rb") as stream:
        body = stream.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("evaluation input limit exceeded")
    value = json.loads(body)
    if not isinstance(value, dict) or value.get("schema") != "nanolab-soak-v1":
        raise ValueError("unsupported evaluation schema")
    return value


def _inputs(
    root: Path,
) -> tuple[dict[str, Any], tuple[Criterion, ...], dict[str, dict[str, Any]]]:
    data = _document(root / "evaluation-input.json")
    if data.get("scope") != "numerical-projection" or data.get("purpose") not in (
        "smoke",
        "p24",
    ):
        raise ValueError("explicit numerical projection and run purpose required")
    raw_criteria = data.get("criteria")
    if not isinstance(raw_criteria, list) or not 1 <= len(raw_criteria) <= 1000:
        raise ValueError("invalid evaluation criterion count")
    criteria = tuple(Criterion.model_validate(item) for item in raw_criteria)
    if len({item.id for item in criteria}) != len(criteria):
        raise ValueError("duplicate evaluation criterion")
    targets = data.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 128:
        raise ValueError("invalid evaluation target count")
    by_role = {}
    for raw in targets:
        target = Target(**raw)
        if (
            target.role in by_role
            or type(target.process_id) is not int
            or target.process_id <= 0
            or any(
                not isinstance(getattr(target, key), str) or not getattr(target, key)
                for key in (
                    "role",
                    "container_id",
                    "process_started_at",
                    "image_digest",
                    "runtime",
                )
            )
        ):
            raise ValueError("invalid or duplicate evaluation target")
        by_role[target.role] = asdict(target)
    if any(item.role not in by_role for item in criteria):
        raise ValueError("criterion refers to unbound role")
    gap, interval = data.get("max_observation_gap_s"), data.get("sample_interval_s")
    if not _number(gap) or not _number(interval) or not 0 < interval <= gap:
        raise ValueError("invalid sampling coverage policy")
    windows = data.get("windows")
    if not isinstance(windows, dict) or set(windows) != {"baseline", "steady", "drain"}:
        raise ValueError("natural baseline, steady and drain windows required")
    previous = -math.inf
    for phase in ("baseline", "steady", "drain"):
        window = windows[phase]
        if not isinstance(window, dict) or set(window) != {"start_s", "end_s"}:
            raise ValueError("invalid natural window")
        start, end = window["start_s"], window["end_s"]
        if not _number(start) or not _number(end) or not previous <= start < end:
            raise ValueError("non-monotone natural windows")
        previous = end
    for item in criteria:
        if item.phase not in windows:
            raise ValueError("criterion must select a natural phase")
        window = windows[item.phase]
        duration = window["end_s"] - window["start_s"]
        if item.window_s > duration or (
            item.deadline_s is not None and item.deadline_s > duration
        ):
            raise ValueError("criterion window/deadline exceeds natural phase")
    perturbations = data.get("perturbations", [])
    if not isinstance(perturbations, list) or len(perturbations) > 1000:
        raise ValueError("invalid perturbation windows")
    for item in perturbations:
        if (
            not isinstance(item, dict)
            or item.get("role") not in by_role
            or not _number(item.get("started_s"))
            or (
                item.get("ended_s") is not None
                and (
                    not _number(item["ended_s"]) or item["ended_s"] < item["started_s"]
                )
            )
        ):
            raise ValueError("invalid perturbation window")
    return data, criteria, by_role


def _index(
    connection: sqlite3.Connection,
    root: Path,
    policy: dict[str, Any],
    targets: dict[str, dict[str, Any]],
) -> set[str]:
    connection.executescript("""
        PRAGMA cache_size=-4096;
        PRAGMA temp_store=FILE;
        CREATE TABLE samples (
            role TEXT, phase TEXT, scheduled REAL, metric TEXT, labels TEXT,
            unit TEXT, value REAL, available INTEGER,
            PRIMARY KEY (role, phase, scheduled, metric, labels)
        );
        CREATE INDEX series ON samples(role, metric, labels, phase, scheduled);
    """)
    issues: set[str] = set()
    last: dict[str, tuple[float, int]] = {}
    phases = {
        "preflight": 0,
        "warmup": 1,
        "baseline": 2,
        "steady": 3,
        "drain": 4,
        "diagnostic": 5,
    }
    stream = root / "samples.jsonl"
    if stream.is_symlink():
        return {"symlink sample stream"}
    try:
        for row in read_records(stream):
            if row.get("kind") == "observation_gap":
                issues.add("torn sample stream")
                continue
            try:
                if row.get("schema") != "nanolab-soak-v1":
                    raise ValueError("unsupported sample schema")
                target = row["target"]
                role = target["role"]
                if role not in targets or target != targets[role]:
                    issues.add("process/container identity changed or unbound")
                    continue
                phase = row["phase"]
                scheduled, started, ended = (
                    row["scheduled_s"],
                    row["started_s"],
                    row["ended_s"],
                )
                if (
                    phase not in phases
                    or not all(_number(item) for item in (scheduled, started, ended))
                    or not scheduled <= started <= ended
                ):
                    raise ValueError("invalid sample timing")
                if phase != "diagnostic":
                    # `diagnostic` is stamped on samples taken while a capture
                    # runs, which takes far longer than one interval. It is
                    # ranked above `drain` because at the final capture it
                    # follows drain, but a capture also runs between the
                    # baseline window and steady, where its ticks would arrive
                    # before later phases and read as non-monotone. It is a
                    # perturbation, not a phase of the measurement, so it is
                    # compared against nothing and advances no cursor.
                    prior = last.get(role)
                    if prior is not None and (
                        scheduled < prior[0] or phases[phase] < prior[1]
                    ):
                        issues.add("non-monotone phase/sample timeline")
                        continue
                    last[role] = (scheduled, phases[phase])
                if phase not in policy["windows"]:
                    continue
                # Where the label may legitimately appear, which is wider than
                # the criterion window when one label spans two sub-phases.
                window = policy.get("phase_extents", {}).get(
                    phase, policy["windows"][phase]
                )
                if (
                    not window["start_s"]
                    <= scheduled
                    <= started
                    <= ended
                    <= window["end_s"]
                ):
                    issues.add("sample crosses natural phase boundary")
                    continue
                if any(
                    item["role"] == role
                    and ended >= item["started_s"]
                    and (item.get("ended_s") is None or started <= item["ended_s"])
                    for item in policy.get("perturbations", [])
                ):
                    issues.add("natural sample overlaps diagnostic perturbation")
                    continue
                labels = row["labels"]
                if (
                    not isinstance(labels, list)
                    or len(labels) > 128
                    or any(
                        not isinstance(pair, list)
                        or len(pair) != 2
                        or not all(isinstance(part, str) for part in pair)
                        for pair in labels
                    )
                    or len({pair[0] for pair in labels}) != len(labels)
                ):
                    raise ValueError("invalid sample labels")
                if not isinstance(row["metric"], str) or not isinstance(
                    row["unit"], str
                ):
                    raise ValueError("invalid metric/unit")
                available = row["availability"]
                value = row["value"]
                if available not in ("observed", "unavailable", "not_applicable"):
                    raise ValueError("invalid sample availability")
                if (available == "observed" and not _number(value)) or (
                    available != "observed" and value is not None
                ):
                    raise ValueError("invalid sample value")
                connection.execute(
                    "INSERT INTO samples VALUES(?,?,?,?,?,?,?,?)",
                    (
                        role,
                        phase,
                        scheduled,
                        row["metric"],
                        json.dumps(sorted(labels), separators=(",", ":")),
                        row["unit"],
                        value,
                        int(available == "observed"),
                    ),
                )
            except (
                ValueError,
                TypeError,
                KeyError,
                OverflowError,
                sqlite3.IntegrityError,
            ):
                issues.add("malformed, nonfinite or duplicate sample")
        connection.commit()
    except (OSError, ValueError):
        issues.add("unreadable or corrupt sample stream")
    return issues


def _window(
    connection: sqlite3.Connection,
    criterion: Criterion,
    labels: str,
    phase: str,
    low: float,
    high: float,
    policy: dict[str, Any],
) -> dict[str, Any]:
    query = """SELECT scheduled, value, unit, available FROM samples
        WHERE role=? AND metric=? AND labels=? AND phase=? AND scheduled BETWEEN ? AND ?
        ORDER BY scheduled"""
    args = (criterion.role, criterion.metric, labels, phase, low, high)
    count, maximum, previous, largest_gap, missing = 0, None, low, 0.0, False
    for scheduled, value, unit, available in connection.execute(query, args):
        if not available or unit != criterion.unit:
            missing = True
            continue
        count += 1
        maximum = value if maximum is None else max(maximum, value)
        largest_gap = max(largest_gap, scheduled - previous)
        previous = scheduled
    largest_gap = max(largest_gap, high - previous)
    minimum_count = required_sample_count(high - low, policy["sample_interval_s"])
    complete = (
        count >= minimum_count
        and not missing
        and largest_gap <= policy["max_observation_gap_s"]
    )
    median = None
    if count:
        # ORDER BY spills to SQLite's temporary files instead of Python lists.
        values = connection.execute(
            """SELECT value FROM samples WHERE
            role=? AND metric=? AND labels=? AND phase=? AND scheduled BETWEEN ? AND ?
            AND available=1 AND unit=? ORDER BY value LIMIT ? OFFSET ?""",
            (*args, criterion.unit, 2 if count % 2 == 0 else 1, (count - 1) // 2),
        ).fetchall()
        median = sum(row[0] for row in values) / len(values)
    return {"complete": complete, "maximum": maximum, "median": median}


def _criterion(
    connection: sqlite3.Connection, criterion: Criterion, policy: dict[str, Any]
) -> CriterionResult:
    window = policy["windows"][criterion.phase]
    high = (
        window["start_s"] + criterion.deadline_s
        if criterion.deadline_s is not None
        else window["end_s"]
    )
    low = high - criterion.window_s
    baseline = policy["windows"]["baseline"]
    seen, failed, incomplete, growth = 0, False, False, False
    details = ""
    for (labels,) in connection.execute(
        "SELECT DISTINCT labels FROM samples WHERE role=? AND metric=? ORDER BY labels",
        (criterion.role, criterion.metric),
    ):
        selector = dict(json.loads(labels))
        if any(
            selector.get(key) != value
            for key, value in criterion.label_selector.items()
        ):
            continue
        seen += 1
        final = _window(
            connection, criterion, labels, criterion.phase, low, high, policy
        )
        incomplete |= not final["complete"]
        maximum = final["maximum"]
        if maximum is None:
            continue
        if criterion.operation == "maximum":
            failed |= maximum > criterion.threshold
        elif criterion.operation == "expected_zero":
            nonzero = connection.execute(
                """SELECT 1 FROM samples WHERE role=? AND metric=? AND labels=?
                AND phase=? AND scheduled BETWEEN ? AND ?
                AND available=1 AND unit=? AND value != 0 LIMIT 1""",
                (
                    criterion.role,
                    criterion.metric,
                    labels,
                    criterion.phase,
                    low,
                    high,
                    criterion.unit,
                ),
            ).fetchone()
            failed |= nonzero is not None
        else:
            reference = _window(
                connection,
                criterion,
                labels,
                "baseline",
                baseline["start_s"],
                baseline["end_s"],
                policy,
            )
            incomplete |= not reference["complete"]
            if not reference["complete"]:
                continue
            median = reference["median"]
            if criterion.operation == "return_to_reference":
                tolerance = min(
                    criterion.absolute_tolerance,
                    abs(median) * criterion.relative_tolerance,
                )
                failed |= maximum > median + tolerance
                details = (
                    f"baseline median={median:g}; natural maximum={maximum:g}; "
                    f"tolerance={tolerance:g}"
                )
            else:
                growth |= maximum - median > criterion.threshold
    if not seen:
        return CriterionResult(
            criterion.id,
            "INCONCLUSIVE",
            "required role/metric/label series absent",
            ("samples.jsonl",),
        )
    if failed:
        status, reason = "FAIL", "observed numerical limit violation"
    elif incomplete:
        status, reason = (
            "INCONCLUSIVE",
            "missing reference/final samples, units or excessive observation gap",
        )
    elif growth:
        status, reason = (
            "INCONCLUSIVE",
            "growth exceeds review threshold; "
            "equal-work evidence and attribution required",
        )
    else:
        status, reason = (
            "PASS",
            "numerical window checks passed; not full-run acceptance",
        )
    if details:
        reason += "; " + details
    return CriterionResult(
        criterion.id, status, reason, ("evaluation-input.json", "samples.jsonl")
    )


def evaluate_run(
    run_dir: Path, attribution_path: Path | None = None
) -> tuple[CriterionResult, ...]:
    """Evaluate numerical windows and full-run receipts using saved artifacts only."""
    results = []
    acceptance = None
    try:
        policy, criteria, targets = _inputs(run_dir)
        with TemporaryDirectory(prefix="nanolab-soak-evaluation-") as temporary:
            connection = sqlite3.connect(str(Path(temporary) / "samples.sqlite"))
            try:
                issues = _index(connection, run_dir, policy, targets)
                results.extend(
                    _criterion(connection, item, policy) for item in criteria
                )
                results.append(
                    CriterionResult(
                        "sample-integrity",
                        "INCONCLUSIVE" if issues else "PASS",
                        "; ".join(sorted(issues))
                        if issues
                        else "indexed sample identities/timelines valid",
                        ("samples.jsonl",),
                    )
                )
                acceptance = evaluate_acceptance(
                    run_dir,
                    policy,
                    tuple(results),
                    attribution_path,
                    connection=connection,
                )
            finally:
                connection.close()
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        results.append(
            CriterionResult(
                "evaluation-input",
                "INCONCLUSIVE",
                str(error)[:2048],
                ("evaluation-input.json", "samples.jsonl"),
            )
        )
    if acceptance is None:
        acceptance = evaluate_acceptance(
            run_dir, None, tuple(results), attribution_path
        )
    replacements = {item.criterion_id: item for item in acceptance}
    merged = []
    for item in results:
        replacement = replacements.pop(item.criterion_id, None)
        if (
            replacement is not None
            and item.status == "INCONCLUSIVE"
            and item.reason.startswith("growth exceeds review threshold")
            and replacement.status == "PASS"
        ):
            merged.append(replacement)
        else:
            merged.append(item)
            if replacement is not None:
                merged.append(
                    CriterionResult(
                        "acceptance." + replacement.criterion_id,
                        replacement.status,
                        replacement.reason,
                        replacement.evidence,
                    )
                )
    return tuple(merged) + tuple(replacements.values())
