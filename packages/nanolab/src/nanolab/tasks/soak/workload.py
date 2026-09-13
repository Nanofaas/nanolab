"""Constant SYNC load, frozen provenance and quota-bounded owned execution."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from threading import Event, Lock

from nanolab.tasks.soak.processes import OwnedCommandRunner


def _json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _positive(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


def constant_arrival_options(
    rate: float, duration_s: float, vus: int, max_vus: int | None = None
) -> dict[str, object]:
    """Represent decimal rates exactly; reject time units over one hour.

    For example 0.35 arrivals/s is rate=7,timeUnit=20s, without truncation.
    Rates requiring a denominator over 3600 or a numerator over 2**31-1 are
    rejected explicitly rather than approximated. ``vus`` is preallocation.
    """
    _positive(duration_s, "duration_s")
    _positive(rate, "rate")
    maximum = vus if max_vus is None else max_vus
    for name, value in (("vus", vus), ("max_vus", maximum)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if maximum < vus:
        raise ValueError("max_vus must be at least vus")
    fraction = Fraction(str(rate))
    if fraction.denominator > 3600 or fraction.numerator > 2**31 - 1:
        raise ValueError(
            "rate requires a timeUnit over 3600s or an oversized integer rate"
        )
    return {
        "executor": "constant-arrival-rate",
        "rate": fraction.numerator,
        "timeUnit": f"{fraction.denominator}s",
        "duration": f"{Decimal(str(duration_s)):f}s",
        "preAllocatedVUs": vus,
        "maxVUs": maximum,
    }


def allocate_vus(
    function_rates: Mapping[str, float], vus: int, max_vus: int | None = None
) -> dict[str, dict[str, int]]:
    """Conserve global VUs with deterministic rate-weighted largest remainders.

    Each function gets one preallocated VU. Remaining preallocation and then
    additional maximum capacity are distributed proportionally to configured
    rates; equal remainders are ordered by function name. Budgets too small for
    one VU per function are explicitly unrepresentable, never rounded upward.
    """
    maximum = vus if max_vus is None else max_vus
    if (
        not function_rates
        or any(not isinstance(name, str) or not name for name in function_rates)
        or type(vus) is not int
        or type(maximum) is not int
        or vus < len(function_rates)
        or maximum < vus
    ):
        raise ValueError(
            "global preallocation must cover every function "
            "and max_vus must cover preallocation"
        )
    for rate in function_rates.values():
        _positive(rate, "rate")
    weights = {
        name: Fraction(str(function_rates[name])) for name in sorted(function_rates)
    }
    total = sum(weights.values())

    def distribute(budget):
        quotas = {name: budget * weight / total for name, weight in weights.items()}
        shares = {name: math.floor(quota) for name, quota in quotas.items()}
        order = sorted(weights, key=lambda name: (-(quotas[name] - shares[name]), name))
        for name in order[: budget - sum(shares.values())]:
            shares[name] += 1
        return shares

    preallocated = distribute(vus - len(weights))
    extra = distribute(maximum - vus)
    return {
        name: {
            "preAllocatedVUs": 1 + preallocated[name],
            "maxVUs": 1 + preallocated[name] + extra[name],
        }
        for name in weights
    }


def _scheduled_demand(function_rates: Mapping[str, float], duration_s: float) -> dict:
    """One absolute boundary iteration per scenario, never percentage underload.

    offered + dropped must differ from rate * duration by at most one iteration.
    Exact rational arithmetic keeps fractional rates and boundary rounding honest.
    Scheduled demand is not server admission; admission stays independently unknown.
    """
    result = {}
    for name, rate in function_rates.items():
        expected = Fraction(str(rate)) * Fraction(str(duration_s))
        result[name] = {
            "rate": rate,
            "duration_s": duration_s,
            "expected_numerator": expected.numerator,
            "expected_denominator": expected.denominator,
            "boundary_tolerance_iterations": 1,
            "minimum_iterations": max(0, math.ceil(expected - 1)),
            "maximum_iterations": math.floor(expected + 1),
            "observed_iterations": None,
        }
    return result


def _counter(metrics: object, name: str) -> dict[str, object]:
    metric = metrics.get(name) if isinstance(metrics, dict) else None
    values = metric.get("values") if isinstance(metric, dict) else None
    value = values.get("count") if isinstance(values, dict) else None
    count: int | None = None
    if (type(value) is int or type(value) is float) and (
        0 <= value <= 2**53 - 1 and int(value) == value
    ):
        count = int(value)
    valid = count is not None
    return {
        "value": count,
        "availability": "observed" if valid else "unavailable",
        "source": name,
    }


def _counters(metrics: object, suffix: str = "") -> dict[str, object]:
    result: dict[str, object] = {
        name: _counter(metrics, "soak_" + name + suffix)
        for name in ("offered", "success", "error", "retry", "replay")
    }
    result["dropped"] = _counter(metrics, "dropped_iterations" + suffix)
    result["admitted"] = {
        "value": None,
        "availability": "unavailable",
        "source": "requires server admission observations; HTTP status is insufficient",
    }
    return result


def _validate_counts(receipt: dict) -> None:
    totals = receipt["counters"]
    per_function = receipt["per_function"]
    groups = {"aggregate": totals, **per_function}
    for group, counts in groups.items():
        if any(
            counter["availability"] != "observed"
            for name, counter in counts.items()
            if name != "admitted"
        ):
            receipt["errors"].append(
                f"required generator counters unavailable: {group}"
            )
            continue
        if (
            counts["offered"]["value"]
            != counts["success"]["value"] + counts["error"]["value"]
        ):
            receipt["errors"].append(f"offered != success + error: {group}")
    for name in ("offered", "success", "error", "retry", "replay", "dropped"):
        values = [counts[name]["value"] for counts in per_function.values()]
        if (
            totals[name]["value"] is not None
            and all(value is not None for value in values)
            and totals[name]["value"] != sum(values)
        ):
            receipt["errors"].append(f"aggregate != per-function sum: {name}")
    for name, demand in receipt["scheduled_demand"].items():
        counts = per_function[name]
        offered, dropped = counts["offered"]["value"], counts["dropped"]["value"]
        if offered is None or dropped is None:
            receipt["errors"].append(f"scheduled demand cannot be verified: {name}")
            continue
        observed = offered + dropped
        demand["observed_iterations"] = observed
        if not demand["minimum_iterations"] <= observed <= demand["maximum_iterations"]:
            receipt["errors"].append(
                f"scheduled demand mismatch: {name}: offered+dropped={observed}, "
                f"required {demand['minimum_iterations']}.."
                f"{demand['maximum_iterations']} "
                "(at most one boundary iteration)"
            )


class K6WorkloadDriver:
    """Local k6; payload cases require exact ``{input, expected}`` fixtures.

    ``artifact_limit_bytes`` caps this output directory, counting existing files,
    frozen inputs, log/summary output and space reserved for final receipts.
    Give each driver a dedicated directory and its allocated remaining budget.
    Image digests must come from runtime preflight; this class never builds.
    """

    def __init__(
        self,
        *,
        base_url: str,
        function_rates: Mapping[str, float],
        payloads: Mapping[str, Sequence[Mapping[str, object]]],
        image_digests: Mapping[str, str],
        config: Mapping[str, object],
        vus: int,
        max_vus: int | None = None,
        artifact_limit_bytes: int = 16 * 1024 * 1024,
        command: Sequence[str] = ("k6",),
        graceful_stop_s: float = 5.0,
        request_timeout_s: float = 30.0,
    ):
        """Freeze payloads, images, global VUs, and the bounded generator policy."""
        if not re.fullmatch(r"https?://[^\s]+", base_url):
            raise ValueError("base_url must be an explicit HTTP endpoint")
        if not function_rates or set(function_rates) != set(payloads):
            raise ValueError("every function requires a rate and payload cases")
        for name, rate in function_rates.items():
            if not isinstance(name, str) or not name:
                raise ValueError("function names must be nonempty strings")
            constant_arrival_options(rate, 1, vus, max_vus)
            if not payloads[name] or any(
                not isinstance(case, Mapping)
                or "input" not in case
                or "expected" not in case
                for case in payloads[name]
            ):
                raise ValueError("each payload requires input and expected result")
        allocate_vus(function_rates, vus, max_vus)
        if not image_digests or any(
            not isinstance(digest, str)
            or not re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", digest)
            for digest in image_digests.values()
        ):
            raise ValueError("image_digests must contain frozen sha256 identities")
        for key in (
            "async_share",
            "asyncShare",
            "idempotency_share",
            "idempotencyShare",
            "idempotency_keys",
        ):
            if config.get(key):
                raise ValueError("main soak forbids async traffic and idempotency keys")
        if (
            isinstance(command, str)
            or not command
            or any(
                not isinstance(arg, str) or not arg or "\0" in arg for arg in command
            )
        ):
            raise ValueError("command must be a nonempty argv sequence")
        if type(artifact_limit_bytes) is not int or artifact_limit_bytes < 16384:
            raise ValueError("artifact_limit_bytes must reserve at least 16384 bytes")
        _positive(graceful_stop_s, "graceful_stop_s")
        _positive(request_timeout_s, "request_timeout_s")
        self._inputs = _json(
            {
                "base_url": base_url.rstrip("/"),
                "function_rates": dict(function_rates),
                "payloads": dict(payloads),
                "image_digests": dict(image_digests),
                "config": dict(config),
                "vus": vus,
                "max_vus": vus if max_vus is None else max_vus,
            }
        )
        self._script = (
            Path(__file__).resolve().parents[4] / "assets/k6/soak-workload.js"
        ).read_bytes()
        self._command = tuple(command)
        self._grace = float(graceful_stop_s)
        self._request_timeout = float(request_timeout_s)
        self._limit = artifact_limit_bytes
        self._runner: OwnedCommandRunner | None = None
        self._run_lock = Lock()
        self._stop_requested = Event()

    def stop(self, timeout_s: float) -> None:
        """Delegate to the shared verified descendant owner; idempotent."""
        _positive(timeout_s, "timeout_s")
        self._stop_requested.set()
        runner = self._runner
        if runner is not None:
            runner.stop(timeout_s)

    def run(self, output_dir: Path, duration_s: float, cancelled: Event) -> Path:
        """Run a workload and persist its conservation and cleanup receipt."""
        _positive(duration_s, "duration_s")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("driver already running")
        try:
            self._stop_requested.clear()
            self._runner = None
            return self._run(Path(output_dir), duration_s, cancelled)
        finally:
            self._run_lock.release()

    def _run(self, output_dir: Path, duration_s: float, cancelled: Event) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = output_dir / "workload-receipt.json"
        names = (
            "workload-receipt.json",
            "workload-receipt.tmp",
            "workload-config.json",
            "workload-inputs.json",
            "soak-workload.js",
            "k6.log",
            "k6-summary.json",
            "generator-process.json",
        )
        if any((output_dir / name).exists() for name in names):
            raise FileExistsError("refusing to overwrite existing workload artifacts")
        inputs = json.loads(self._inputs)
        functions = []
        allocation = allocate_vus(
            inputs["function_rates"], inputs["vus"], inputs["max_vus"]
        )
        for index, (name, rate) in enumerate(inputs["function_rates"].items()):
            own = allocation[name]
            options = constant_arrival_options(
                rate, duration_s, own["preAllocatedVUs"], own["maxVUs"]
            )
            options.update(
                {"exec": "invoke", "gracefulStop": f"{self._request_timeout:g}s"}
            )
            functions.append(
                {
                    "name": name,
                    "scenario": f"fn_{index}",
                    "options": options,
                    "payloads": inputs["payloads"][name],
                }
            )
        effective = {
            "base_url": inputs["base_url"],
            "functions": functions,
            "request_timeout_s": self._request_timeout,
        }
        summary_path = output_dir / "k6-summary.json"
        provenance = {
            "function_rates": inputs["function_rates"],
            "image_digests": inputs["image_digests"],
            "image_identity_source": "upstream runtime preflight",
            "config_sha256": _hash(self._inputs),
            "script_sha256": _hash(self._script),
            "payload_sha256": {
                name: _hash(_json(cases)) for name, cases in inputs["payloads"].items()
            },
            "effective_config_sha256": _hash(_json(effective)),
            "command": list(self._command),
            "retry_scope": "client only; server retries require server metrics",
            "offered_scope": (
                "iterations that attempted HTTP; "
                "scheduled demand and drops are separate"
            ),
        }
        receipt = {
            "schema": "nanolab-soak-v1",
            "kind": "workload",
            "started_s": time.monotonic(),
            "generator_end_s": None,
            "duration_s": duration_s,
            "completed": False,
            "cancelled": cancelled.is_set(),
            "forced_stop": False,
            "timed_out": False,
            "cleanup_complete": False,
            "quota_exceeded": False,
            "artifact_limit_bytes": self._limit,
            "threshold_failed": False,
            "exit_code": None,
            "errors": [],
            "provenance": provenance,
            "vu_policy": {
                "scope": "global",
                "preallocated_vus": inputs["vus"],
                "max_vus": inputs["max_vus"],
                "allocation": allocation,
            },
            "scheduled_demand": _scheduled_demand(inputs["function_rates"], duration_s),
            "counters": _counters({}),
            "per_function": {name: _counters({}) for name in inputs["function_rates"]},
        }
        # Reserve two full final receipts for atomic replacement plus process
        # metadata and bounded error growth BEFORE allowing any generator output.
        receipt_ceiling = 2 * len(_json(receipt)) + 8192
        reserve = 2 * receipt_ceiling + 8192
        existing = sum(
            path.stat().st_size for path in output_dir.rglob("*") if path.is_file()
        )
        fixed = {
            "soak-workload.js": self._script,
            "workload-config.json": _json(effective),
            "workload-inputs.json": self._inputs,
        }
        output_budget = self._limit - existing - sum(map(len, fixed.values())) - reserve
        if output_budget <= 0:
            raise ValueError(
                "artifact budget cannot fit frozen inputs and reserved final receipts"
            )
        receipt["output_limit_bytes"] = output_budget

        def persist():
            encoded = _json(receipt)
            if len(encoded) > receipt_ceiling:
                raise ValueError("final receipt exceeds its reserved artifact budget")
            temporary = receipt_path.with_suffix(".tmp")
            temporary.write_bytes(encoded)
            temporary.replace(receipt_path)

        persist()
        failure = None
        result = None
        try:
            for name, content in fixed.items():
                (output_dir / name).write_bytes(content)
            if not cancelled.is_set() and not self._stop_requested.is_set():
                executable = shutil.which(self._command[0])
                if executable is None:
                    raise FileNotFoundError(
                        f"generator executable missing: {self._command[0]}"
                    )
                with Path(executable).open("rb") as binary:
                    provenance["executable_sha256"] = hashlib.file_digest(
                        binary, "sha256"
                    ).hexdigest()
                provenance["executable_path"] = executable
                marker = "NANOLAB_" + secrets.token_hex(16)
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("K6_")
                }
                env.update(
                    NANOLAB_SOAK_CONFIG=str(
                        (output_dir / "workload-config.json").resolve()
                    ),
                    NANOLAB_SOAK_SUMMARY_MARKER=marker,
                    K6_NO_USAGE_REPORT="true",
                    K6_WEB_DASHBOARD="false",
                )
                self._runner = OwnedCommandRunner(
                    [
                        executable,
                        *self._command[1:],
                        "run",
                        "--quiet",
                        str((output_dir / "soak-workload.js").resolve()),
                    ],
                    cwd=output_dir.resolve(),
                    env=env,
                    log_path=output_dir / "k6.log",
                    timeout_s=duration_s + self._request_timeout,
                    cancelled=cancelled,
                    output_limit_bytes=output_budget,
                    stop_timeout_s=self._grace,
                    summary_path=summary_path,
                    summary_marker=marker,
                )
                if self._stop_requested.is_set():
                    self._runner.stop(self._grace)
                result = self._runner.run()
                (output_dir / "generator-process.json").write_bytes(
                    _json(
                        {
                            "schema": "nanolab-soak-v1",
                            "kind": "owned-process",
                            **asdict(result),
                        }
                    )
                )
                receipt.update(
                    exit_code=result.returncode,
                    forced_stop=result.forced_stop,
                    timed_out=result.timed_out,
                    quota_exceeded=result.quota_exceeded,
                    cleanup_complete=result.reaped,
                    generator_end_s=result.ended_s,
                    log_bytes=result.log_bytes,
                    summary_bytes=result.summary_bytes,
                )
                receipt["errors"].extend(result.errors)
                if result.quota_exceeded:
                    receipt["errors"].append(
                        "generator output exceeded artifact budget"
                    )
            else:
                receipt["cleanup_complete"] = True
                receipt["generator_end_s"] = time.monotonic()
        except BaseException as exc:
            failure = exc
            receipt["errors"].append(f"{type(exc).__name__}: {exc}"[:512])
        finally:
            receipt["cancelled"] = (
                cancelled.is_set()
                or self._stop_requested.is_set()
                or bool(result and result.cancelled)
            )
            try:
                if result is None or not result.summary_complete:
                    raise ValueError("complete framed summary unavailable")
                with summary_path.open("rb") as source:
                    raw = source.read(min(output_budget, 8 * 1024 * 1024) + 1)
                if len(raw) > min(output_budget, 8 * 1024 * 1024):
                    raise ValueError("summary exceeds bounded parser capacity")
                metrics = json.loads(raw)["metrics"]
                receipt["counters"] = _counters(metrics)
                receipt["per_function"] = {
                    item["name"]: _counters(
                        metrics, "{scenario:" + item["scenario"] + "}"
                    )
                    for item in functions
                }
                _validate_counts(receipt)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                receipt["errors"].append(f"summary unavailable: {exc}"[:512])
            receipt["threshold_failed"] = receipt["exit_code"] == 99
            receipt["completed"] = (
                receipt["exit_code"] in (0, 99)
                and receipt["cleanup_complete"]
                and not any(
                    receipt[key]
                    for key in (
                        "cancelled",
                        "forced_stop",
                        "timed_out",
                        "quota_exceeded",
                        "errors",
                    )
                )
            )
            persist()
        if failure is not None:
            raise failure
        if receipt["exit_code"] not in (None, 0, 99) and not any(
            receipt[key]
            for key in ("cancelled", "timed_out", "quota_exceeded", "forced_stop")
        ):
            raise RuntimeError(
                f"k6 driver crashed with exit code {receipt['exit_code']}; "
                f"evidence: {receipt_path}"
            )
        return receipt_path
