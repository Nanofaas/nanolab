"""Conservative sustained capacity selection from complete raw trial groups."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

CAPACITY_FACTORS = (0.5, 0.7, 0.9, 1.0, 1.1, 1.25)


def comparison_replica_vectors(
    settings, aliases: dict[str, str]
) -> dict[str, dict[str, int]]:
    """Use one feasible balanced vector for strict concurrent baseline and cloud."""
    from nanolab.config.one_shot import resolve_node_functions

    if settings.strict_memory and len(settings.functions) > 1:
        return next(
            entry["vectors"]
            for entry in capacity_configurations(settings, aliases)
            if entry["kind"] == "mixed" and entry["dominant"] is None
        )
    return {
        node.id: {
            aliases[name]: fn.max_replicas
            for name, fn in resolve_node_functions(settings, node.id).items()
        }
        for node in settings.nodes
    }


def capacity_configurations(settings, aliases: dict[str, str]) -> list[dict]:
    """Predeclare every pure integer level and six feasible concurrent mixes."""
    from nanolab.config.one_shot import resolve_node_functions

    local = {
        node.id: {
            aliases[name]: fn
            for name, fn in resolve_node_functions(settings, node.id).items()
        }
        for node in settings.nodes
    }
    names = list(aliases.values())
    configurations = []
    for name in names:
        maximum = max(functions[name].max_replicas for functions in local.values())
        configurations.extend(
            {
                "kind": "pure",
                "function": name,
                "level": level,
                "vectors": {
                    node: {other: level if other == name else 0 for other in names}
                    for node, functions in local.items()
                    if level <= functions[name].max_replicas
                },
            }
            for level in range(1, maximum + 1)
        )
    seen = set()
    for dominant in [None, *names]:
        vectors = {}
        for node in settings.nodes:
            functions = local[node.id]
            vector = dict.fromkeys(names, 1)
            remaining = node.memory_capacity_mib - sum(
                fn.memory_mib for fn in functions.values()
            )
            if remaining < 0:
                raise ValueError("initial concurrent functions exceed memory pool")
            order = names if dominant is None else [dominant]
            while True:
                grew = False
                for name in order:
                    fn = functions[name]
                    if fn.memory_mib <= remaining and vector[name] < fn.max_replicas:
                        vector[name] += 1
                        remaining -= fn.memory_mib
                        grew = True
                if not grew:
                    break
            vectors[node.id] = vector
        signature = json.dumps(vectors, sort_keys=True)
        if signature not in seen:
            seen.add(signature)
            configurations.append(
                {"kind": "mixed", "dominant": dominant, "vectors": vectors}
            )
    return configurations


def select_capacity(
    trials: list[dict], *, node: str, replicas: int, tolerance: float
) -> float:
    """Choose a tested rate only when all three 60-second trials sustain it."""
    groups = defaultdict(list)
    for trial in trials:
        if trial["node"] == node and trial["replicas"] == replicas:
            groups[trial["rate"]].append(trial)
    supported = []
    for rate, rows in groups.items():
        if (
            not math.isfinite(rate)
            or rate <= 0
            or len(rows) != 3
            or {row["repetition"] for row in rows} != {0, 1, 2}
        ):
            continue
        if all(
            row["warmupSeconds"] >= 30
            and row["measurementSeconds"] >= 60
            and row["planned"] > 0
            and row["emitted"] == row["planned"]
            and row["physicalCompletions"] / row["planned"] >= 0.99
            and row["generatorDeficit"] == row["censored"] == 0
            and row["validOutput"]
            and row["relativeServiceDrift"] <= tolerance
            for row in rows
        ):
            supported.append(rate)
    if not supported:
        raise ValueError(f"NOT_QUALIFIED: no sustained capacity for {node}/{replicas}")
    return max(supported)


def select_mixed_capacity(
    trials: list[dict],
    *,
    node: str,
    vector: dict[str, int],
    rates: dict[str, float],
    tolerance: float,
) -> dict[str, float]:
    """Qualify an exact concurrent configuration, never combine isolated maxima."""
    if (
        set(vector) != set(rates)
        or not vector
        or any(type(count) is not int or count < 0 for count in vector.values())
        or any(
            type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0
            for rate in rates.values()
        )
        or any((vector[name] > 0) != (rates[name] > 0) for name in vector)
    ):
        raise ValueError("NOT_QUALIFIED: invalid concurrent capacity scope")
    active = {name for name, count in vector.items() if count}
    if not active:
        raise ValueError("NOT_QUALIFIED: empty concurrent capacity scope")
    selected = [
        row
        for row in trials
        if row["node"] == node
        and row.get("replicaVector") == vector
        and row.get("mix") == rates
    ]
    if (
        len(selected) != 3 * len(active)
        or {row.get("function") for row in selected} != active
    ):
        raise ValueError("NOT_QUALIFIED: incomplete concurrent capacity")
    for name in active:
        rows = [row for row in selected if row["function"] == name]
        if (
            len(rows) != 3
            or {row["repetition"] for row in rows} != {0, 1, 2}
            or any(
                row["replicas"] != vector[name]
                or row["rate"] != rates[name]
                or not isinstance(row.get("maxArrivalLatenessSeconds"), (float, int))
                or not 0 <= row["maxArrivalLatenessSeconds"] <= 0.25
                for row in rows
            )
        ):
            raise ValueError("NOT_QUALIFIED: concurrent repetition/rate mismatch")
        select_capacity(rows, node=node, replicas=vector[name], tolerance=tolerance)
    return dict(rates)


def read_concurrent_capacity(
    raw: dict,
    *,
    root: Path,
    nodes: set[str],
    functions: set[str],
    fingerprint: str,
    tolerance: float,
) -> list[dict]:
    """Verify exact joint rates and all three immutable raw trial artifacts."""
    from nanolab.config.one_shot import ArtifactReference
    from nanolab.one_shot.profiles import relative_reference

    if (
        raw.get("schemaVersion") != 2
        or raw.get("artifactType") != "measured-concurrent-capacity"
        or raw.get("environmentFingerprint") != fingerprint
        or not isinstance(raw.get("functions"), list)
        or len(raw["functions"]) != len(functions)
        or set(raw["functions"]) != functions
    ):
        raise ValueError("concurrent capacity scope/fingerprint mismatch")
    records = raw.get("capacities")
    if not isinstance(records, list) or not records:
        raise ValueError("missing concurrent capacity records")
    covered, signatures = set(), set()
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"node", "replicaVector", "rates", "trials"}
            or record["node"] not in nodes
            or not isinstance(record["replicaVector"], dict)
            or not isinstance(record["rates"], dict)
            or set(record["replicaVector"]) != functions
            or set(record["rates"]) != functions
            or not isinstance(record["trials"], list)
            or len(record["trials"]) != 3
        ):
            raise ValueError("invalid concurrent capacity record scope")
        signature = (
            record["node"],
            json.dumps(record["replicaVector"], sort_keys=True),
            json.dumps(record["rates"], sort_keys=True),
        )
        if signature in signatures:
            raise ValueError("duplicate concurrent capacity record")
        signatures.add(signature)
        trials, paths = [], set()
        for value in record["trials"]:
            reference = relative_reference(
                root, ArtifactReference.model_validate(value)
            )
            if reference.path in paths:
                raise ValueError("duplicate concurrent trial reference")
            paths.add(reference.path)
            rows = json.loads(reference.read_verified())
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) for row in rows
            ):
                raise ValueError("invalid concurrent trial rows")
            trials.extend(rows)
        try:
            select_mixed_capacity(
                trials,
                node=record["node"],
                vector=record["replicaVector"],
                rates=record["rates"],
                tolerance=tolerance,
            )
        except (KeyError, TypeError) as error:
            raise ValueError("invalid concurrent trial fields") from error
        if all(record["replicaVector"].values()):
            covered.add(record["node"])
    if covered != nodes:
        raise ValueError("concurrent capacity missing full function mix for a node")
    return records
