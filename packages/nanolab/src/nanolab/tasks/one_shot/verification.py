"""Independent checks of SDK identity, routing and calibrated physical service."""

from __future__ import annotations

from collections import defaultdict


def validate_proof(proof: dict, execution_id: str) -> bool:
    """Require exact SDK identity and runtime incarnation, never a latency proxy."""
    return (
        proof.get("executionId") == execution_id
        and bool(proof.get("incarnation"))
        and str(proof.get("dispatchAttempt", "")) == "1"
    )


def verify_observations(
    attempts,
    results,
    *,
    profile,
    tolerance,
    nodes,
    endpoints=None,
    require_execution_node=True,
):
    """Detect service drift separately per destination/function and routing mismatch."""
    violations = []
    demand = {row["function"]: row["serviceSeconds"] for row in profile.functions}
    durations = defaultdict(list)
    outcome = {row["originalId"]: row for row in results}
    for proof in attempts:
        destination, name = proof.get("destination"), proof["function"]
        if destination not in nodes:
            violations.append("physical destination is outside frozen topology")
        row = outcome.get(proof["originalId"], {})
        if (require_execution_node or row.get("executionNode") is not None) and row.get(
            "executionNode"
        ) != destination:
            violations.append("terminal destination disagrees with physical proof")
        if endpoints is not None:
            target = row.get("offloadedTarget")
            if destination == row.get("origin"):
                if target:
                    violations.append(
                        "local physical execution has a forwarding target"
                    )
            elif target != endpoints.get(destination):
                violations.append(
                    "forwarding target differs from terminal destination: "
                    "possible extra hop"
                )
        if str(proof.get("dispatchAttempt")) != "1":
            violations.append("dispatch attempt differs from single-attempt contract")
        value = proof.get("occupancySeconds")
        if proof.get("handlerStarted") and proof.get("state") == "RELEASED":
            if not isinstance(value, (float, int)) or value <= 0:
                violations.append("physical occupancy missing or invalid")
            else:
                durations[(destination, name)].append(value)
    observed = {}
    for (node, name), values in durations.items():
        mean = sum(values) / len(values)
        drift = abs(mean / demand[name] - 1)
        observed[f"{node}/{name}"] = {
            "samples": len(values),
            "meanSeconds": mean,
            "drift": drift,
        }
        profile_limit = (
            next(row for row in profile.functions if row["function"] == name)
            .get("validity", {})
            .get("maxRelativeCapacityError", tolerance)
        )
        if drift > min(tolerance, profile_limit):
            violations.append(
                f"physical service drift exceeds tolerance: {node}/{name}"
            )
    if not durations:
        violations.append("physical service drift cannot be measured")
    return sorted(set(violations)), observed


def verify_runtime_inventory(rows, *, functions, nodes):
    """Verify observed physical c1 and declared container limits."""
    violations = []
    memory = defaultdict(int)
    for row in rows:
        status = row["status"]
        fn = functions[row["function"]]
        limits = row["container"]["HostConfig"]
        if row.get("at") and row.get("node"):
            memory[(row["at"], row["node"])] += limits.get("Memory", 0)
        if (
            status.get("maxConcurrentHandlers") != 1
            or status.get("activeHandlers", 2) > 1
            or not status.get("physicalReleaseProof")
        ):
            violations.append("runtime physical c1/release-proof assumption violated")
        if limits.get("Memory") != fn.memory_mib * 1024 * 1024 or limits.get(
            "NanoCpus"
        ) != int(fn.cpu * 1e9):
            violations.append("runtime resource limits differ from frozen function")
    for (_, node), used in memory.items():
        if used > nodes[node].config.memory_capacity_mib * 1024 * 1024:
            violations.append("observed replica RAM exceeds frozen node pool")
    if not rows:
        violations.append("runtime physical inventory unavailable")
    return sorted(set(violations))


def verify_epoch_flows(plans: dict) -> None:
    """Match acknowledged one-hop peer assignments and conserve forecast rates."""
    for node, plan in plans.items():
        for name, function in plan["functions"].items():
            outbound = sum(
                row["quantity"] * plan["flowQuantum"]
                for row in function["outbound"]
                if row["readyConfirmed"]
            )
            if (
                abs(
                    function["forecastRate"]
                    - function["localRate"]
                    - function["cloudRate"]
                    - outbound
                )
                > 1e-8
            ):
                raise ValueError("ready forecast allocation does not conserve flow")
            for direction, counterpart in [
                ("outbound", "inbound"),
                ("inbound", "outbound"),
            ]:
                for row in function[direction]:
                    if not row["readyConfirmed"]:
                        continue
                    peer = (
                        row["sellerId"] if direction == "outbound" else row["buyerId"]
                    )
                    if peer == node or peer not in plans:
                        raise ValueError("peer assignment outside one-hop topology")
                    other = plans[peer]["functions"].get(name, {}).get(counterpart, [])
                    if row not in other:
                        raise ValueError("peer ready confirmation is not reciprocal")


def realized_utility(cells, *, functions, cloud) -> float | None:
    """Normalize realized routing utility using the solver's zero-price convention."""
    planned = sum(cell["planned"] for cell in cells)
    if not planned:
        return None
    welfare = 0.0
    for cell in cells:
        fn = functions[cell["function"]]
        destinations = cell["destinations"]
        local = destinations.get(cell["origin"], 0)
        terminal = destinations.get(cloud, 0)
        peer = sum(destinations.values()) - local - terminal
        welfare += (
            fn.alpha * local + fn.delta * peer - fn.gamma * (terminal + cell["errors"])
        )
    return welfare / planned


def verify_warmup(schedule, rows, *, max_lateness):
    """Account for warmup arrivals and HTTP outcomes separately from SDK completions."""
    planned = {
        row["originalId"]: row for row in schedule if row.get("phase") == "warmup"
    }
    emitted = [
        row
        for row in rows
        if row.get("phase") == "warmup" and row.get("event") == "emitted"
    ]
    returned = [
        row
        for row in rows
        if row.get("phase") == "warmup" and row.get("event") == "result"
    ]
    emitted_ids = [row["originalId"] for row in emitted]
    returned_ids = [row["originalId"] for row in returned]
    valid = bool(
        planned
        and len(emitted_ids) == len(set(emitted_ids)) == len(planned)
        and len(returned_ids) == len(set(returned_ids)) == len(planned)
        and set(emitted_ids) == set(returned_ids) == set(planned)
    )
    for row in emitted:
        lateness = row["emittedAt"] - planned.get(row["originalId"], row)["scheduledAt"]
        valid = valid and -0.001 <= lateness <= max_lateness
    for row in returned:
        valid = (
            valid
            and row.get("finishedAt", -1) >= row["emittedAt"]
            and 0 < row.get("statusCode", 0) < 600
        )
    return {
        "planned": len(planned),
        "emitted": len(emitted),
        "returned": len(returned),
        "httpErrors": sum(
            row.get("statusCode", 0) >= 400
            or str(row.get("responseStatus", "")).upper() == "ERROR"
            for row in returned
        ),
        "valid": bool(valid),
        "basis": "generator HTTP outcomes; separate from campaign SDK completions",
    }
