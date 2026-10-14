"""Validate and apply offline calendars without changing k6 or ORACLE means."""

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from nanolab.tasks.one_shot.trace import TraceArtifact


def _key(cell):
    return (
        cell.get("phase"),
        cell.get("epoch"),
        cell.get("origin"),
        cell.get("function"),
    )


def validate_arrival_profile(
    document: dict, trace: TraceArtifact, *, trace_sha256: str, warmup_seconds: float
) -> None:
    """Check complete scope, source identity, counts and finite ordered offsets."""
    if not isinstance(document, dict):
        raise ValueError("arrival profile must be an object")
    version = document.get("schemaVersion")
    if type(version) is not int or version not in (1, 2):
        raise ValueError("unsupported arrival profile schemaVersion")
    if version == 2:
        scale = document.get("curveTimeScaleSeconds")
        if (
            isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not math.isfinite(scale)
            or scale <= 0
        ):
            raise ValueError("invalid arrival profile curve time scale")
    for name, value in [
        ("periodSeconds", trace.period_seconds),
        ("epochs", len(trace.windows)),
        ("warmupSeconds", warmup_seconds),
    ]:
        actual = document.get(name)
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or actual != value
        ):
            raise ValueError("arrival profile field mismatch: " + name)
    if document.get("traceSha256") != trace_sha256:
        raise ValueError("arrival profile trace hash mismatch")
    expected = {}
    for epoch, window in enumerate(trace.windows):
        for node, rates in window.items():
            for function, rate in rates.items():
                expected[("campaign", epoch, node, function)] = (
                    round(rate * trace.period_seconds),
                    epoch * trace.period_seconds,
                    (epoch + 1) * trace.period_seconds,
                )
    for node, rates in trace.windows[0].items():
        for function, rate in rates.items():
            expected[("warmup", -1, node, function)] = (
                round(rate * warmup_seconds),
                -warmup_seconds,
                0,
            )
    cells = document.get("cells")
    if not isinstance(cells, list):
        raise ValueError("arrival profile cells must be a list")
    seen = set()
    for cell in cells:
        if (
            not isinstance(cell, dict)
            or type(cell.get("epoch")) is not int
            or not all(
                isinstance(cell.get(n), str) for n in ("phase", "origin", "function")
            )
        ):
            raise ValueError("invalid arrival profile cell identity")
        key = _key(cell)
        if key in seen or key not in expected:
            raise ValueError("duplicate or extra arrival profile cell")
        seen.add(key)
        count, start, end = expected[key]
        offsets = cell.get("offsetsSeconds")
        if not isinstance(offsets, list) or (version == 1 and len(offsets) != count):
            raise ValueError("arrival profile cell request count mismatch")
        previous = None
        for offset in offsets:
            if (
                isinstance(offset, bool)
                or not isinstance(offset, (int, float))
                or not math.isfinite(offset)
                or not start <= offset < end
                or (previous is not None and offset <= previous)
            ):
                raise ValueError("invalid arrival profile offset")
            previous = offset
    if seen != set(expected):
        raise ValueError("arrival profile scope is incomplete")


def retime_schedule(
    rows: list[dict], document: dict, *, anchor_timestamp: float
) -> list[dict]:
    """Change only emission times, preserving original cell order and identities."""
    groups = defaultdict(list)
    for row in rows:
        groups[_key(row)].append(row)
    cells = {_key(cell): cell for cell in document["cells"]}
    if len(cells) != len(document["cells"]) or set(groups) != {
        key for key, cell in cells.items() if cell["offsetsSeconds"]
    }:
        raise ValueError("arrival profile schedule scope mismatch")
    result = []
    for key, originals in groups.items():
        originals = sorted(originals, key=lambda row: row["scheduledAt"])
        offsets = cells[key]["offsetsSeconds"]
        if len(originals) != len(offsets):
            raise ValueError("arrival profile schedule count mismatch")
        for row, offset in zip(originals, offsets, strict=True):
            result.append(dict(row, scheduledAt=anchor_timestamp + offset))
    return sorted(result, key=lambda row: (row["scheduledAt"], row["originalId"]))


def read_frozen_arrival_profile(
    directory: Path, frozen: dict, *, warmup_seconds: float = 720
) -> dict | None:
    """Read only a self-contained hashed profile from its frozen directory."""
    identity = frozen.get("arrivalProfile")
    if identity is None:
        if "arrival-profile.json" in frozen.get("files", {}):
            raise ValueError("arrival profile identity is missing")
        return None
    path = directory / "arrival-profile.json"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if (
        not isinstance(identity, dict)
        or identity.get("sha256") != digest
        or frozen.get("files", {}).get("arrival-profile.json") != digest
    ):
        raise ValueError("arrival profile frozen hash mismatch")
    document = json.loads(path.read_text())
    trace_path = directory / "trace.json"
    trace = TraceArtifact.model_validate_json(trace_path.read_bytes())
    validate_arrival_profile(
        document,
        trace,
        trace_sha256=hashlib.sha256(trace_path.read_bytes()).hexdigest(),
        warmup_seconds=warmup_seconds,
    )
    return document


def schedule_from_arrival_profile(
    trace: TraceArtifact,
    document: dict,
    *,
    anchor,
    endpoints: dict[str, str],
    run_id: str,
) -> list[dict]:
    """Build originals from a validated replay calendar, independently of forecasts."""
    rows = []
    for cell in document["cells"]:
        phase, epoch, node, name = _key(cell)
        for index, offset in enumerate(cell["offsetsSeconds"]):
            rows.append(
                {
                    "originalId": f"{run_id}:{phase}:{epoch}:{node}:{name}:{index}",
                    "origin": node,
                    "function": name,
                    "epoch": epoch,
                    "phase": phase,
                    **(
                        {"originRole": trace.node_roles[node]}
                        if trace.node_roles
                        else {}
                    ),
                    **(
                        {"phaseId": trace.phase_ids[epoch]}
                        if trace.phase_ids and epoch >= 0
                        else {}
                    ),
                    "scheduledAt": anchor.timestamp() + offset,
                    "url": endpoints[node] + f"/v1/functions/{name}:invoke",
                    "input": trace.payloads[name],
                }
            )
    return sorted(rows, key=lambda row: (row["scheduledAt"], row["originalId"]))
