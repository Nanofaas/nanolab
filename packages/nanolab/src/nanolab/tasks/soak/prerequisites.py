"""Verifiable P24 short-profile receipts and an injected, isolated execution API.

No infrastructure is started by importing this module. ``runner(coverage, id)``
returns an async context manager owning a fresh platform lifetime, never the
subsequent measured platform. Its session implements ``identities()``,
``exercise(coverage)`` and ``populations()``. These async methods return observed
image digests, raw behavioral observations and role/population counts. Adapters
must obtain these from the target; HTTP completion cannot replace populations.

Inputs contain ``images`` (all application roles), ``relevant_config`` keyed by
exact coverage IDs, artifact descriptors ``payload``/``script``, and
``settlement``: coverage -> role -> population -> {limit, retention_s}. The
settlement deadline belongs to the profile, not the run: the two outcome
populations settle at the lifetime of the key that profile's own exercise used,
so a keyed profile is never judged by the keyless deadline. The entire input
object is fingerprinted; expected fingerprints must come from frozen run inputs.
Output expectations belong to each relevant_config entry's ``expected_output``.
Coverage-specific raw observation fields are documented in _behavior below.

Receipts use schema nanolab-soak-v1, kind prerequisites, purpose p24, input fingerprint,
exact coverage, per-profile assertions/timing and hashed JSONL evidence. Hashes
provide integrity and provenance matching, not signatures or source authenticity.
The profile adapter and the original evidence producer remain trusted observers.
"""

from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
import os
import re
import signal
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from copy import deepcopy
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import Any, Protocol, TypeGuard
from uuid import uuid4

from nanolab.tasks.soak.artifacts import (
    MAX_RECORD_BYTES,
    ArtifactWriter,
    describe_artifact,
    fingerprint,
    read_records,
)
from nanolab.tasks.soak.models import CriterionResult

SCHEMA = "nanolab-soak-v1"
SUPPORTED_COVERAGE = frozenset(
    {
        "sync",
        "error",
        "timeout",
        "cancellation",
        "async",
        "late-callback",
        "idempotent-replay",
        "function-name-churn",
    }
)
_SOAK_POPULATIONS = MappingProxyType(
    {
        "control-plane": frozenset(
            {
                "execution_records",
                "outcomes",
                "logical_executions",
                "canonical_input_bytes",
                "physical_input_copy_bytes",
                "expiry_queue_depth",
                "pending_acquisitions",
                "replica_snapshots",
            }
        ),
        "java": frozenset({"live_executions"}),
        "javascript": frozenset({"live_executions", "input_bytes", "output_bytes"}),
    }
)
_SOAK_ROLE_KINDS = MappingProxyType(
    {
        "control-plane": "control-plane",
        "java": "java",
        "javascript": "javascript",
        "word-stats-java": "java",
        "word-stats-javascript": "javascript",
    }
)
_COVERAGE_POPULATIONS = MappingProxyType(
    {
        "async": MappingProxyType(
            {
                "java": frozenset({"callbacks", "callback_bytes"}),
                "javascript": frozenset(
                    {"callbacks", "callback_bytes", "serialized_callback_bytes"}
                ),
            }
        ),
        "late-callback": MappingProxyType(
            {
                "java": frozenset({"callbacks", "callback_bytes"}),
                "javascript": frozenset(
                    {"callbacks", "callback_bytes", "serialized_callback_bytes"}
                ),
            }
        ),
        "cancellation": MappingProxyType({"control-plane": frozenset({"waiters"})}),
        "idempotent-replay": MappingProxyType(
            {"control-plane": frozenset({"idempotency_entries"})}
        ),
        "function-name-churn": MappingProxyType(
            {"control-plane": frozenset({"retired_owners"})}
        ),
    }
)


class ProfileSession(Protocol):
    """Observe real behavior while the caller-owned isolated platform is alive."""

    async def identities(self) -> dict[str, str]:
        """Return observed immutable image identities for every application role."""
        ...

    async def exercise(self, coverage: str) -> dict[str, object]:
        """Return after logical completion; physical work may still own state."""
        ...

    async def populations(self) -> dict[str, dict[str, float]]:
        """Observe retained population counts separately for each application role."""
        ...


ProfileRunner = Callable[[str, str], AbstractAsyncContextManager[ProfileSession]]


def _number(value: object) -> TypeGuard[float]:
    """Report whether this is a finite, non-negative measurement."""
    if type(value) is not int and type(value) is not float:
        return False
    return math.isfinite(value) and value >= 0


def _artifact(descriptor: object) -> str:
    if not isinstance(descriptor, dict):
        raise ValueError("missing artifact descriptor")
    path = Path(descriptor["path"])
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("artifact must identify an existing absolute regular file")
    actual = describe_artifact(path)
    if any(descriptor.get(key) != actual[key] for key in actual):
        raise ValueError(f"artifact identity/hash mismatch: {path}")
    return str(path)


def _required_populations(
    images: dict[str, str],
    coverage: frozenset[str],
    metrics_profile: str,
) -> dict[str, frozenset[str]]:
    if metrics_profile not in {"advanced", "soak"}:
        raise ValueError(f"unsupported metrics profile: {metrics_profile}")
    if metrics_profile == "advanced":
        return {role: frozenset() for role in images}
    unknown = set(images) - set(_SOAK_ROLE_KINDS)
    if unknown:
        raise ValueError(f"unsupported soak role: {', '.join(sorted(unknown))}")
    required = {role: _SOAK_POPULATIONS[_SOAK_ROLE_KINDS[role]] for role in images}
    coverage_ids = set(coverage)
    if "error-timeout-cancellation" in coverage:
        coverage_ids.update({"error", "timeout", "cancellation"})
    if "async-late-callback" in coverage:
        coverage_ids.update({"async", "late-callback"})
    for coverage_id in coverage_ids:
        for kind, populations in _COVERAGE_POPULATIONS.get(coverage_id, {}).items():
            for role in required:
                if _SOAK_ROLE_KINDS[role] == kind:
                    required[role] |= populations
    return required


def normalize_prerequisite_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Normalize heterogeneous prerequisite inputs for deterministic execution."""
    normalized = deepcopy(inputs)
    normalized.setdefault("metrics_profile", "advanced")
    return normalized


def select_relevant_config(config: Mapping[str, Any], key: str) -> Any:
    """Read one declared relevant-config key out of a dumped configuration.

    The key is a dotted path, and it is written back as a single flat key of the
    profile, which is how acceptance reads it. Freezing and checking therefore
    share this one selection rather than each spelling the walk themselves.
    """
    value: Any = config
    for part in key.split("."):
        value = value[part]
    return deepcopy(value)


def _inputs(inputs: dict, coverage: frozenset[str]) -> None:
    images = inputs["images"]
    if not isinstance(images, dict) or "control-plane" not in images or len(images) < 2:
        raise ValueError("images must include control-plane and every SDK role")
    if any(
        not isinstance(role, str)
        or not role
        or not isinstance(digest, str)
        or re.fullmatch(r"(?:[^\s@]+@)?sha256:[0-9a-f]{64}", digest) is None
        for role, digest in images.items()
    ):
        raise ValueError("all roles require immutable image identities")
    configs = inputs["relevant_config"]
    if not isinstance(configs, dict) or set(configs) != coverage:
        raise ValueError("relevant config must cover exactly the required profiles")
    if any(not isinstance(config, dict) or not config for config in configs.values()):
        raise ValueError("relevant configuration cannot be empty")
    _artifact(inputs["payload"])
    _artifact(inputs["script"])
    policies = inputs["settlement"]
    if not isinstance(policies, dict) or set(policies) != coverage:
        raise ValueError("settlement policies must cover exactly the required profiles")
    required = _required_populations(images, coverage, inputs["metrics_profile"])
    for roles in policies.values():
        if not isinstance(roles, dict) or set(roles) != set(images):
            raise ValueError("settlement policies must cover every image role")
        for role, populations in roles.items():
            if not isinstance(populations, dict) or not required[role].issubset(
                populations
            ):
                raise ValueError("required retained-population policies missing")
            for name, policy in populations.items():
                if (
                    not isinstance(name, str)
                    or not name
                    or not isinstance(policy, dict)
                ):
                    raise ValueError("invalid population policy")
                if set(policy) != {"limit", "retention_s"} or not all(
                    _number(v) for v in policy.values()
                ):
                    raise ValueError(
                        "population policy requires finite limit and retention_s"
                    )
                if name == "live_executions" and policy["limit"] != 0:
                    raise ValueError("physical handlers must settle to zero")


def _assertion(name: str, observed: object, expected: object, passed: bool) -> dict:
    return {
        "id": name,
        "observed": observed,
        "expected": expected,
        "status": "PASS" if passed else "FAIL",
    }


def _behavior(coverage: str, observed: dict, config: dict) -> list[dict]:
    """Evaluate profile semantics from raw observations, never PASS flags."""

    def equal(field, expected):
        value = observed[field]
        return _assertion(
            field, value, expected, type(value) is type(expected) and value == expected
        )

    if coverage == "sync":
        return [equal("http_status", 200), equal("output", config["expected_output"])]
    if coverage == "error":
        status = observed["http_status"]
        response = observed.get("response")
        terminal = observed.get("execution")
        expected_code = config.get("expected_error_code")
        terminal_error = (
            isinstance(expected_code, str)
            and bool(expected_code.strip())
            and isinstance(response, dict)
            and isinstance(terminal, dict)
            and response.get("status") == terminal.get("status") == "error"
            and isinstance(response.get("executionId"), str)
            and bool(response["executionId"].strip())
            and response["executionId"] == terminal.get("executionId")
            and isinstance(response.get("error"), dict)
            and isinstance(terminal.get("error"), dict)
            and response["error"].get("code")
            == terminal["error"].get("code")
            == expected_code
        )
        return [
            equal("outcome", "ERROR"),
            _assertion(
                "http_status",
                status,
                "400..599 or 200 with matching frozen terminal error",
                type(status) is int
                and (400 <= status <= 599 or (status == 200 and terminal_error)),
            ),
        ]
    if coverage in {"timeout", "cancellation"}:
        return [equal("outcome", "TIMEOUT" if coverage == "timeout" else "CANCELLED")]
    if coverage == "async":
        return [
            equal("enqueue_status", 202),
            equal("execution_status", "SUCCESS"),
            equal("output", config["expected_output"]),
        ]
    if coverage == "late-callback":
        return [
            equal("outcome", "TIMEOUT"),
            equal("callback_status", "ignored"),
            equal("callback_after_timeout", True),
        ]
    if coverage == "idempotent-replay":
        ids = observed["execution_ids"]
        same = (
            isinstance(ids, list)
            and len(ids) >= 2
            and all(isinstance(v, str) and v for v in ids)
            and len(set(ids)) == 1
        )
        return [
            _assertion("execution_ids", ids, "same nonempty identity", same),
            equal("outputs", [config["expected_output"]] * len(ids)),
        ]
    if coverage == "function-name-churn":
        created, removed = observed["created_names"], observed["removed_names"]
        valid = (
            isinstance(created, list)
            and isinstance(removed, list)
            and all(isinstance(v, str) and v for v in [*created, *removed])
            and len(created) >= 2
            and len(set(created)) == len(created)
            and len(removed) == len(created)
            and set(created) == set(removed)
        )
        return [
            _assertion(
                "removed_names", observed, "all distinct created names removed", valid
            )
        ]
    raise ValueError(f"unsupported coverage: {coverage}")


def _evaluate(raw: dict, inputs: dict) -> tuple[list[dict], str, str]:
    if raw.get("error"):
        return [], "INCONCLUSIVE", str(raw["error"])
    if (
        raw.get("recovery_required") is True
        and raw.get("recovery", {}).get("cleanup_confirmed") is not True
    ):
        raise ValueError("parent recovery cleanup is unconfirmed")
    coverage = raw["coverage"]
    if raw["images"] != inputs["images"]:
        raise ValueError("effective profile images differ from frozen images")
    if raw.get("released") is not True:
        raise ValueError("isolated lifetime release not confirmed")
    times = [raw[key] for key in ("started_s", "logical_finished_s", "ended_s")]
    if not all(_number(t) for t in times) or times != sorted(times):
        raise ValueError("invalid profile timing")
    for phase in ("acquire", "body", "release"):
        stage = raw["stages"][phase]
        start, end, budget = (
            stage[key] for key in ("started_s", "ended_s", "timeout_s")
        )
        if (
            not all(_number(value) for value in (start, end, budget))
            or budget == 0
            or not times[0] <= start <= end <= times[-1]
            or end - start > budget
        ):
            raise ValueError(f"profile exceeded its independent {phase} budget")
    if (
        raw["worker"]["reaped"] is not True
        or raw["worker"]["forced_stop"] is not False
        or raw["worker"]["exit_code"] != 0
    ):
        raise ValueError("profile worker did not finish and reap normally")
    assertions = _behavior(
        coverage, raw["observations"], inputs["relevant_config"][coverage]
    )
    # This profile's own settlement, not the run's: what its exercise retained is
    # held for the lifetime that exercise's key implies.
    policies = inputs["settlement"][coverage]
    expected_keys = {
        (role, name)
        for role, by_population in policies.items()
        for name in by_population
    }
    seen = set()
    for sample in raw["settlement"]:
        key = (sample["role"], sample["population"])
        if key not in expected_keys or key in seen:
            raise ValueError("unexpected or duplicate population evidence")
        seen.add(key)
        policy = policies[key[0]][key[1]]
        when, value = sample["observed_s"], sample["value"]
        if (
            not _number(when)
            or not _number(value)
            or not times[1] + policy["retention_s"] <= when <= times[2]
        ):
            raise ValueError(
                "population missing, non-finite or sampled before retention"
            )
        assertions.append(
            _assertion(
                f"{key[0]}/{key[1]}", value, policy["limit"], value <= policy["limit"]
            )
        )
    if seen != expected_keys:
        raise ValueError("incomplete role/population settlement observations")
    status = "FAIL" if any(a["status"] == "FAIL" for a in assertions) else "PASS"
    return assertions, status, "behavior and physical retained populations evaluated"


def validate_receipt(
    receipt: dict[str, Any],
    expected_fingerprint: str,
    required_coverage: frozenset[str],
) -> CriterionResult:
    """Require exact P24 inputs and re-evaluate hashed evidence offline."""
    evidence = []
    try:
        if (
            receipt.get("schema") != SCHEMA
            or receipt.get("kind") != "prerequisites"
            or receipt.get("purpose") != "p24"
        ):
            raise ValueError(
                "requires a versioned P24 prerequisite receipt; smoke is insufficient"
            )
        if (
            not required_coverage
            or set(receipt["coverage"]) != required_coverage
            or len(receipt["coverage"]) != len(required_coverage)
        ):
            raise ValueError("receipt coverage is not exactly the requested coverage")
        inputs = receipt["inputs"]
        if (
            receipt.get("fingerprint") != expected_fingerprint
            or fingerprint(inputs) != expected_fingerprint
        ):
            raise ValueError("frozen image/config/payload/script fingerprint mismatch")
        _inputs(inputs, required_coverage)
        unsupported = required_coverage - SUPPORTED_COVERAGE
        if unsupported:
            raise ValueError(f"unsupported coverage: {', '.join(sorted(unsupported))}")
        profiles = receipt["profiles"]
        if (
            len(profiles) != len(required_coverage)
            or {p["coverage"] for p in profiles} != required_coverage
        ):
            raise ValueError("missing independent profile evidence")
        lifetimes = set()
        statuses = []
        reasons = []
        previous_end = None
        for profile in profiles:
            path = _artifact(profile["evidence"])
            evidence.append(path)
            terminal = None
            for record in read_records(Path(path)):
                if record.get("kind") == "observation_gap":
                    raise ValueError("incomplete prerequisite evidence")
                if record.get("kind") == "profile":
                    if terminal is not None:
                        raise ValueError("duplicate terminal profile")
                    terminal = record
            if terminal is None:
                raise ValueError("missing terminal profile evidence")
            raw = terminal
            if (
                raw["fingerprint"] != expected_fingerprint
                or raw["coverage"] != profile["coverage"]
            ):
                raise ValueError("profile artifact identity mismatch")
            lifetime = raw["lifetime_id"]
            if not isinstance(lifetime, str) or not lifetime or lifetime in lifetimes:
                raise ValueError("profiles must own distinct isolated lifetimes")
            lifetimes.add(lifetime)
            for key in (
                "lifetime_id",
                "started_s",
                "ended_s",
                "released",
                "cleanup_error",
                "worker",
            ):
                if profile[key] != raw[key]:
                    raise ValueError("receipt timing/lifetime differs from artifact")
            for key in ("recovery_required", "recovery"):
                if profile.get(key) != raw.get(key):
                    raise ValueError("receipt recovery differs from parent evidence")
            if previous_end is not None and raw["started_s"] < previous_end:
                raise ValueError("profile lifetimes overlap")
            previous_end = raw["ended_s"]
            assertions, status, reason = _evaluate(raw, inputs)
            if profile["assertions"] != assertions or profile["status"] != status:
                raise ValueError("assertion results differ from raw profile evidence")
            statuses.append(status)
            reasons.append(f"{profile['coverage']}: {reason}")
        status = (
            "FAIL"
            if "FAIL" in statuses
            else "INCONCLUSIVE"
            if "INCONCLUSIVE" in statuses
            else "PASS"
        )
        return CriterionResult(
            "prerequisites", status, "; ".join(reasons), tuple(evidence)
        )
    except (KeyError, TypeError, ValueError, OSError, OverflowError) as error:
        return CriterionResult(
            "prerequisites", "INCONCLUSIVE", str(error), tuple(evidence)
        )


def _send(connection, value: dict) -> None:
    payload = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError("prerequisite worker message exceeds evidence bound")
    connection.send_bytes(payload)


async def _profile_body(session, coverage, frozen, raw, connection) -> None:
    raw["images"] = await session.identities()
    _send(connection, {"event": "patch", "values": {"images": raw["images"]}})
    if raw["images"] != frozen["images"]:
        raise ValueError("effective profile images differ from frozen images")
    raw["observations"] = await session.exercise(coverage)
    raw["logical_finished_s"] = monotonic()
    _send(
        connection,
        {
            "event": "patch",
            "values": {
                "observations": raw["observations"],
                "logical_finished_s": raw["logical_finished_s"],
            },
        },
    )
    settlement = frozen["settlement"][coverage]
    deadlines = sorted(
        {
            policy["retention_s"]
            for policies in settlement.values()
            for policy in policies.values()
        }
    )
    for delay in deadlines:
        await asyncio.sleep(max(0, raw["logical_finished_s"] + delay - monotonic()))
        populations = await session.populations()
        observed_s = monotonic()
        for role, policies in settlement.items():
            for name, policy in policies.items():
                if policy["retention_s"] == delay:
                    sample = {
                        "role": role,
                        "population": name,
                        "observed_s": observed_s,
                        "value": populations.get(role, {}).get(name),
                    }
                    raw["settlement"].append(sample)
                    _send(connection, {"event": "population", "sample": sample})


async def _worker_lifetime(runner, coverage, frozen, raw, connection, budgets):
    """Cooperative release inside a process that the parent can forcibly reap."""
    main = asyncio.current_task()
    if main is None:
        raise RuntimeError("prerequisites must run inside a task")
    phase = None
    release_permitted = asyncio.Event()

    async def commands():
        while True:
            if connection.poll():
                command = json.loads(connection.recv_bytes(MAX_RECORD_BYTES))
                if command["command"] == "release":
                    release_permitted.set()
                elif command["command"] == "cancel" and command["phase"] == phase:
                    main.cancel()
            await asyncio.sleep(0.005)

    control = asyncio.create_task(commands())
    context = None
    session = None
    cause = None
    acquired = False
    try:
        for phase in ("acquire", "body", "release"):
            if phase == "body" and not acquired:
                continue
            stage = {"started_s": monotonic(), "timeout_s": budgets[phase]}
            raw["stages"][phase] = stage
            _send(connection, {"event": "phase", "phase": phase, "stage": stage})
            try:
                async with asyncio.timeout(budgets[phase]):
                    if phase == "acquire":
                        context = runner(coverage, raw["lifetime_id"])
                        session = await context.__aenter__()
                        acquired = True
                    elif phase == "body":
                        await _profile_body(session, coverage, frozen, raw, connection)
                    else:
                        # Parent persists partial evidence before it permits teardown.
                        await release_permitted.wait()
                        if context is not None:
                            await context.__aexit__(
                                type(cause) if cause else None,
                                cause,
                                cause.__traceback__ if cause else None,
                            )
                        raw["released"] = acquired and context is not None
                        if not raw["released"]:
                            raw["cleanup_error"] = (
                                "acquisition did not complete; "
                                "resource ownership is unconfirmed"
                            )
            except BaseException as error:
                message = f"{phase}: {type(error).__name__}: {error}"
                if phase == "release":
                    raw["cleanup_error"] = message
                if cause is None:
                    cause = error
                    raw["error"] = message
                # A caught cancellation must not consume the independent release budget.
                while main.cancelling():
                    main.uncancel()
            finally:
                stage["ended_s"] = monotonic()
                _send(
                    connection,
                    {
                        "event": "stage-ended",
                        "phase": phase,
                        "stage": stage,
                        "error": raw.get("error"),
                        "cleanup_error": raw["cleanup_error"],
                        "released": raw["released"],
                    },
                )
        raw["ended_s"] = monotonic()
        _send(connection, {"event": "result", "raw": raw})
    finally:
        control.cancel()
        await asyncio.gather(control, return_exceptions=True)


def _profile_worker(
    runner, coverage, frozen, raw, connection, parent_connection, budgets
):
    """Run the adapter without daemonizing children or reusing parent-owned clients."""
    parent_connection.close()
    try:
        os.setsid()
        _send(connection, {"event": "ready", "pid": os.getpid()})
        asyncio.run(
            _worker_lifetime(runner, coverage, frozen, raw, connection, budgets)
        )
    finally:
        connection.close()


def _signal_worker(process, group_owned, sig):
    try:
        if group_owned:
            os.killpg(process.pid, sig)
        else:
            os.kill(process.pid, sig)
    except ProcessLookupError:
        pass


async def _reap_worker(process, group_owned, stop_timeout_s, cancelled, *, force):
    """No coroutine from the adapter runs in this event loop or survives its worker."""
    forced = force

    async def wait_exit():
        deadline = monotonic() + stop_timeout_s
        while process.is_alive() and monotonic() < deadline:
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError as error:
                cancelled.append(error)
        process.join(0)
        return not process.is_alive()

    finished = False if force else await wait_exit()
    if not finished:
        forced = True
        _signal_worker(process, group_owned, signal.SIGTERM)
        finished = await wait_exit()
    if not finished:
        _signal_worker(process, group_owned, signal.SIGKILL)
        finished = await wait_exit()
    result = {
        "pid": process.pid,
        "reaped": finished,
        "forced_stop": forced,
        "exit_code": process.exitcode,
    }
    if finished:
        process.close()
    return result


async def _supervise_profile(
    runner,
    coverage,
    frozen,
    raw,
    writer,
    stream,
    budgets,
    stop_timeout_s,
    *,
    before_fork=None,
    recover_after_reap=None,
):
    """Persist from the parent and enforce phase bounds outside adapter Python code."""
    cancelled = []
    parent = child = process = None
    started = False
    group_owned = False
    force = False
    partial_written = False
    stage = "acquire"
    deadline = monotonic() + budgets[stage]
    stop_deadline = None

    def partial():
        nonlocal partial_written
        if not partial_written:
            writer.write_json(
                f"prerequisite-{raw['lifetime_id']}-partial.json",
                {
                    "schema": SCHEMA,
                    "kind": "prerequisites",
                    "purpose": "p24",
                    "status": "INCONCLUSIVE",
                    "profile": raw,
                },
            )
            partial_written = True

    def command(value):
        if parent is not None:
            with suppress(OSError, EOFError):
                _send(parent, value)

    try:
        if "fork" not in multiprocessing.get_all_start_methods() or os.name != "posix":
            raise ValueError(
                "unsupported prerequisite runner platform: "
                "owned POSIX fork worker required"
            )
        context = multiprocessing.get_context("fork")
        if before_fork is not None:
            before_fork(raw["lifetime_id"], writer)
        parent, child = context.Pipe()
        process = context.Process(
            target=_profile_worker,
            args=(runner, coverage, frozen, deepcopy(raw), child, parent, budgets),
            name=f"prerequisite-{raw['lifetime_id']}",
        )
        process.start()
        started = True
        child.close()
        while True:
            while parent.poll():
                event = json.loads(parent.recv_bytes(MAX_RECORD_BYTES))
                kind = event["event"]
                if kind == "ready":
                    group_owned = event["pid"] == process.pid
                elif kind == "phase":
                    stage = event["phase"]
                    raw["stages"][stage] = event["stage"]
                    deadline = event["stage"]["started_s"] + budgets[stage]
                    stop_deadline = None
                    if stage == "release":
                        partial()
                        command({"command": "release"})
                    elif cancelled:
                        command({"command": "cancel", "phase": stage})
                        stop_deadline = monotonic() + stop_timeout_s
                elif kind == "patch":
                    raw.update(event["values"])
                elif kind == "population":
                    raw["settlement"].append(event["sample"])
                elif kind == "stage-ended":
                    raw["stages"][event["phase"]] = event["stage"]
                    if event.get("error"):
                        raw.setdefault("error", event["error"])
                    if event.get("cleanup_error"):
                        raw["cleanup_error"] = event["cleanup_error"]
                    raw["released"] = event["released"]
                elif kind == "result":
                    for key, value in event["raw"].items():
                        if key != "error" or "error" not in raw:
                            raw[key] = value
                    return cancelled
                else:
                    raise ValueError("unsupported worker evidence event")
                writer.append(stream, {"kind": "worker-event", **event})
            if not process.is_alive():
                raise RuntimeError(
                    "prerequisite worker exited without terminal evidence"
                )
            now = monotonic()
            if stop_deadline is not None and now >= stop_deadline:
                force = True
                raw.setdefault(
                    "error",
                    f"{stage}: worker did not stop within cancellation deadline",
                )
                if not raw["released"]:
                    raw["cleanup_error"] = (
                        f"{stage}: forced worker stop; resource release unconfirmed"
                    )
                break
            if now >= deadline and stop_deadline is None:
                raw.setdefault("error", f"{stage}: independent deadline exceeded")
                if stage == "release":
                    raw["cleanup_error"] = (
                        "release: independent deadline exceeded; "
                        "resource release unconfirmed"
                    )
                partial()
                command({"command": "cancel", "phase": stage})
                stop_deadline = now + stop_timeout_s
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError as error:
                cancelled.append(error)
                raw.setdefault("error", f"{stage}: externally cancelled")
                partial()
                if stage != "release":
                    command({"command": "cancel", "phase": stage})
                    if stop_deadline is None:
                        stop_deadline = monotonic() + stop_timeout_s
    except (Exception, asyncio.CancelledError) as error:
        if isinstance(error, asyncio.CancelledError):
            cancelled.append(error)
        raw.setdefault("error", f"{stage}: {type(error).__name__}: {error}")
        force = started
    finally:
        # Even evidence-write errors cannot bypass owned worker shutdown.
        try:
            partial()
        finally:
            if started:
                raw["worker"] = await _reap_worker(
                    process, group_owned, stop_timeout_s, cancelled, force=force
                )
                if raw["worker"]["forced_stop"]:
                    raw.setdefault(
                        "error",
                        "worker required forced stop; adapter left unfinished work",
                    )
                if not raw["worker"]["reaped"]:
                    raw["error"] = (
                        "owned worker did not reap after SIGKILL; "
                        "unresolved kernel/process failure"
                    )
                if recover_after_reap is not None:
                    _parent_recovery(raw, recover_after_reap)
                if not raw["released"]:
                    raw["cleanup_error"] = (
                        raw["cleanup_error"]
                        or "resource release unconfirmed; recover by lifetime_id"
                    )
            if parent is not None:
                parent.close()
            if child is not None:
                child.close()
            if cancelled:
                raw.setdefault(
                    "error", "profile externally cancelled, including worker shutdown"
                )
            raw["ended_s"] = monotonic()
    return cancelled


def _parent_recovery(raw, recover_after_reap) -> None:
    """Recover only after the supervisor's real join/close, never worker claims."""
    raw["recovery_required"] = True
    worker = raw["worker"]
    result = {
        "lifetime_id": raw["lifetime_id"],
        "cleanup_confirmed": False,
        "status": "unconfirmed",
        "reason": "supervisor did not confirm worker reap",
        "evidence": None,
    }
    if (
        worker.get("reaped") is True
        and type(worker.get("pid")) is int
        and worker["pid"] > 0
        and type(worker.get("exit_code")) is int
    ):
        try:
            recovery = recover_after_reap(raw["lifetime_id"], deepcopy(worker))
            if recovery.lifetime_id != raw["lifetime_id"]:
                raise ValueError("recovery belongs to another lifetime")
            result.update(
                {
                    "cleanup_confirmed": recovery.cleanup_confirmed is True,
                    "status": recovery.status,
                    "reason": recovery.reason,
                    "evidence": recovery.evidence,
                }
            )
        except BaseException as error:
            result["reason"] = f"parent recovery: {type(error).__name__}: {error}"
    raw["recovery"] = result
    if not result["cleanup_confirmed"]:
        raw.setdefault("error", "parent recovery cleanup is unconfirmed")
        raw["cleanup_error"] = result["reason"]


async def run_prerequisites(
    *,
    inputs: dict[str, object],
    required_coverage: frozenset[str],
    runner: ProfileRunner,
    writer: ArtifactWriter,
    timeout_s: float = 60.0,
    acquire_timeout_s: float = 60.0,
    release_timeout_s: float = 10.0,
    stop_timeout_s: float = 1.0,
    before_fork: Callable | None = None,
    recover_after_reap: Callable | None = None,
) -> dict[str, object]:
    """Execute each adapter in a fresh owned POSIX subprocess with parent supervision.

    ``timeout_s`` bounds body execution only. Acquisition and release have separate
    budgets; ``stop_timeout_s`` bounds cancellation, termination and kill/reap waits.
    The runner factory executes inside the worker: construct clients there, own
    resources by lifetime_id, never daemonize commands or reuse measured resources.
    In-memory adapter mutations do not propagate to the parent.

    Cancellation is re-raised after the worker is reaped and an ABORTED receipt is
    written. Failed/hanging acquisition, body, release or driver shutdown cannot
    qualify P24. ``released`` means observed context release, not a killed worker.
    External resources surviving failed release remain explicitly unconfirmed and
    require the workflow's lifetime-based recovery adapter before any measured run.
    Unsupported platforms/coverage are INCONCLUSIVE without running an adapter.
    """
    budgets = {
        "acquire": acquire_timeout_s,
        "body": timeout_s,
        "release": release_timeout_s,
    }
    if any(
        not _number(value) or value == 0
        for value in (*budgets.values(), stop_timeout_s)
    ):
        raise ValueError("phase and stop timeouts must be finite and positive")
    if (before_fork is None) != (recover_after_reap is None):
        raise ValueError(
            "owned prerequisite reservation and recovery hooks must be paired"
        )
    frozen = normalize_prerequisite_inputs(inputs)
    input_fingerprint = fingerprint(frozen)
    receipt = {
        "schema": SCHEMA,
        "kind": "prerequisites",
        "purpose": "p24",
        "inputs": frozen,
        "fingerprint": input_fingerprint,
        "coverage": sorted(required_coverage),
        "profiles": [],
        "status": "INCONCLUSIVE",
    }
    receipt_id = uuid4().hex
    try:
        _inputs(frozen, required_coverage)
        for coverage in sorted(required_coverage):
            lifetime = uuid4().hex
            stream = f"prerequisite-{lifetime}"
            raw = {
                "kind": "profile",
                "coverage": coverage,
                "lifetime_id": lifetime,
                "fingerprint": input_fingerprint,
                "started_s": monotonic(),
                "timeout_s": timeout_s,
                "released": False,
                "cleanup_error": None,
                "settlement": [],
                "stages": {},
                "worker": {
                    "pid": None,
                    "reaped": False,
                    "forced_stop": False,
                    "exit_code": None,
                },
            }
            cancelled = []
            writer.append(
                stream, {"kind": "start", "coverage": coverage, "lifetime_id": lifetime}
            )
            try:
                if coverage not in SUPPORTED_COVERAGE:
                    raise ValueError(f"unsupported coverage: {coverage}")
                cancelled = await _supervise_profile(
                    runner,
                    coverage,
                    frozen,
                    raw,
                    writer,
                    stream,
                    budgets,
                    stop_timeout_s,
                    before_fork=before_fork,
                    recover_after_reap=recover_after_reap,
                )
            except asyncio.CancelledError as error:
                raw.setdefault("error", "profile cancelled before complete settlement")
                cancelled.append(error)
            except Exception as error:
                raw.setdefault("error", f"{type(error).__name__}: {error}")
            finally:
                raw["ended_s"] = monotonic()
                try:
                    assertions, status, reason = _evaluate(raw, frozen)
                except (KeyError, TypeError, ValueError, OverflowError) as error:
                    raw["error"] = str(error)
                    assertions, status, reason = [], "INCONCLUSIVE", str(error)
                writer.append(stream, raw)
                receipt["profiles"].append(
                    {
                        "coverage": coverage,
                        "lifetime_id": lifetime,
                        "started_s": raw["started_s"],
                        "ended_s": raw["ended_s"],
                        "released": raw["released"],
                        "cleanup_error": raw["cleanup_error"],
                        "worker": raw["worker"],
                        **{
                            key: raw[key]
                            for key in ("recovery_required", "recovery")
                            if key in raw
                        },
                        "assertions": assertions,
                        "status": status,
                        "reason": reason,
                        "evidence": describe_artifact(writer.root / f"{stream}.jsonl"),
                    }
                )
            if cancelled:
                receipt["status"] = "ABORTED"
                raise cancelled[0]
            if (
                recover_after_reap is not None
                and raw.get("recovery", {}).get("cleanup_confirmed") is not True
            ):
                # Never start another lifetime while external ownership is unresolved.
                break
        receipt["status"] = validate_receipt(
            receipt, input_fingerprint, required_coverage
        ).status
        return receipt
    except asyncio.CancelledError:
        receipt["status"] = "ABORTED"
        raise
    finally:
        writer.write_json(f"prerequisites-{receipt_id}.json", receipt)
