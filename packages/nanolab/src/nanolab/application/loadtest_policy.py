"""Load-test policy, workload profiles and generator configuration."""

import math
from pathlib import Path

from nanolab.application.functions import resolve_function_definition
from nanolab.config.scenario import ScenarioConfig

_CONCURRENCY_COOLDOWN_MS = 5000
# The ceiling the governor works under: FunctionQueueState clamps the effective
# limit to the function's `concurrency`, so it has to leave room above
# maxTargetInFlightPerPod or the trajectory is flat by construction.
_CONCURRENCY_CEILING = 8
# The buffer in front of that ceiling, registered on every function a concurrency
# run creates. It is named rather than repeated because the burst profile's peak
# is derived from it: the two have to agree or the load stops being calibrated
# against the queue it is meant to fill.
_CONCURRENCY_QUEUE_SIZE = 100
# What every BUDGETED function may hold between them. See `concurrency_budget`
# for why it is neither the per-function ceiling nor the sum of them.
_BURST_TOTAL_BUDGET = 12
# The open-loop peak, per function. Measured saturation on the shared cores was around
# 1,400 requests per second, so this asks for more than the pair can serve: the point of
# the profile is a queue that grows because demand exceeded capacity.
_OPEN_LOOP_PEAK_RPS = 1_800
# The governor only reacts to a function that gets slower under concurrency, and
# on an unconstrained multi-core host word-stats-java does not: an early run
# climbed to the ceiling and stayed there, correctly, because eight parallel
# requests never contended for anything. The cap creates the contention at a
# concurrency the governor can reach. This is controlling the variable, not
# arranging the answer - the run still has to show that it notices and recovers.
#
# Four cores rather than one, because one flattens the very comparison the
# scenarios exist for: with a single core the optimum is 1-2 for every runtime,
# so a JVM and a GIL-bound interpreter look identical. With four, a runtime that
# can use them should settle near four while one that serialises CPU work should
# still settle near one. The ceiling stays above that, or the governor would be
# clamped rather than converging.
_CONCURRENCY_FUNCTION_CPUS = 4
_CONCURRENCY_FUNCTION_RESOURCES: dict[str, dict[str, float]] = {
    "requests": {"cpu": _CONCURRENCY_FUNCTION_CPUS / 2, "memoryMiB": 256},
    "limits": {"cpu": float(_CONCURRENCY_FUNCTION_CPUS), "memoryMiB": 512},
}


def payload_corpus_path(
    config: ScenarioConfig, root: Path, function: str
) -> Path | None:
    """Return the corpus to send, or None to keep the generator's built-in text.

    The corpora live in the nanoFaaS checkout, one per FAMILY rather than per
    function: `word-stats-java` and `word-stats-java-lite` replay the same bytes, so
    a difference between them is the runtime and not the input. See
    docs/loadtest-payload-profile.md in that repository.

    Missing corpus raises rather than falling back. A silent fallback would leave the
    run driving an idle function while reporting success, which is precisely the
    failure this knob exists to prevent.
    """
    if config.payload_profile is None:
        return None
    family = resolve_function_definition(function).family
    corpus = (
        root
        / "functions"
        / "test-data"
        / family
        / f"performance-{config.payload_profile}.json"
    )
    if not corpus.is_file():
        raise FileNotFoundError(
            f"payloadProfile {config.payload_profile!r} needs {corpus}, "
            "which does not exist"
        )
    return corpus


def _concurrency_function_resources(
    config: ScenarioConfig, function: str
) -> dict[str, dict[str, float]]:
    """Return the resource cap one function runs under concurrency control.

    That is what the scenario declared for it, else the default above. The
    default used to be applied unconditionally, so a scenario that declared
    `resources` for a function under concurrency control had them silently discarded
    — while the schema validates those very keys against the selected functions, which
    reads as a promise that they apply.

    It matters more than tidiness because the cap is a CALIBRATION, and a calibration
    goes stale: see the note on _CONCURRENCY_FUNCTION_CPUS. Making it a per-scenario
    value lets one run be re-tuned without moving the constant under every other run
    and breaking comparability with the campaigns already recorded.
    """
    declared = config.resources.get(function)
    if declared is None:
        return _CONCURRENCY_FUNCTION_RESOURCES
    resolved = {
        key: dict(value) for key, value in _CONCURRENCY_FUNCTION_RESOURCES.items()
    }
    for section in ("requests", "limits"):
        quantity = getattr(declared, section, None)
        if quantity is None:
            continue
        if quantity.cpu is not None:
            resolved[section]["cpu"] = float(quantity.cpu)
        if quantity.memory_mib is not None:
            resolved[section]["memoryMiB"] = int(quantity.memory_mib)
    return resolved


def _concurrency_control_setup(config: ScenarioConfig) -> dict[str, object] | None:
    """Return the scaling config for a run with concurrency control.

    Fixed replicas, adaptive per-replica concurrency: `NONE` rather than
    `INTERNAL` so nothing moves the replica count even if an
    autoscaler were present: with one replica pinned, every change in
    `function_effective_concurrency` is the governor's doing.
    """
    if not config.concurrency_control:
        return None
    return {
        "strategy": "NONE",
        "minReplicas": 1,
        "maxReplicas": 1,
        "concurrencyControl": _controller_settings(config),
    }


# The SLO the BUDGETED run is held to. Set from the measured uncontended service
# time of the functions in the catalogue — around 1ms warm — with room for the
# queueing that concurrency legitimately buys, so the target is reachable but not
# free.
_TARGET_LATENCY_MS = 10

# What a caller is promised, end to end. Larger than the service-time SLO on
# purpose: the queue is where a concurrency limit puts the wait it saves, so the
# two numbers cannot be equal without either forbidding queueing altogether or
# lying about one of them.
_END_TO_END_P95_BUDGET_MS = 50

# What the budget above assumes: the generator's built-in ~20-word inputs, whose
# uncontended service time is around 1 ms warm. Selecting a heavier corpus with
# `payloadProfile` changes the amount of work in a request by orders of magnitude
# (5,000 and 50,000 words against 20), and a promise of 50 ms means something else
# entirely for each. Holding the number fixed would turn the threshold into a test
# of "is the payload small" rather than "does the platform keep its promise".
#
# So the budget is restated per profile, keeping the ratio the original calibration
# used - roughly fifty times the uncontended service time - rather than relaxed to
# whatever the last run happened to produce. Measured on the DGX Spark described in
# the nanoFaaS campaign README, medium on two cores gave p95 68.8 ms, which sits
# under this budget with room while still leaving a doubling detectable.
_PAYLOAD_P95_BUDGET_MS: dict[str, int] = {"small": 60, "medium": 120, "large": 600}


def end_to_end_p95_budget_ms(config: ScenarioConfig) -> int:
    """Return the p95 the load generator holds this run to, by payload profile.

    A heavier corpus puts more work in every request, so the budget is restated
    per profile rather than held at one number.
    """
    if config.payload_profile is None:
        return _END_TO_END_P95_BUDGET_MS
    return _PAYLOAD_P95_BUDGET_MS[config.payload_profile]


def _controller_settings(config: ScenarioConfig) -> dict[str, object]:
    if config.concurrency_mode == "SOJOURN":
        # Held to the SAME promise the load generator checks. For this mode
        # `targetLatencyMs` is end-to-end rather than service time, so reusing the
        # service-time SLO would hold the controller to a number it can never
        # reach — the wait alone was measured at four times it — and it would
        # search continuously instead of ever resting.
        return {
            "mode": "SOJOURN",
            "minTargetInFlightPerPod": 1,
            "maxTargetInFlightPerPod": _CONCURRENCY_CEILING,
            "targetLatencyMs": _END_TO_END_P95_BUDGET_MS,
        }
    if config.concurrency_mode == "BUDGETED":
        # No per-replica target and no gradient thresholds: this controller is
        # told what the function must deliver, not how its limit should step.
        return {
            "mode": "BUDGETED",
            "minTargetInFlightPerPod": 1,
            "maxTargetInFlightPerPod": _CONCURRENCY_CEILING,
            "targetLatencyMs": _TARGET_LATENCY_MS,
            "weight": 1.0,
        }
    return {
        "mode": "ADAPTIVE_PER_POD",
        "targetInFlightPerPod": 4,
        "minTargetInFlightPerPod": 1,
        "maxTargetInFlightPerPod": _CONCURRENCY_CEILING,
        "upscaleCooldownMs": _CONCURRENCY_COOLDOWN_MS,
        "downscaleCooldownMs": _CONCURRENCY_COOLDOWN_MS,
    }


def is_co_tenancy(config: ScenarioConfig) -> bool:
    """Whether this run puts two functions on one control plane on purpose.

    Read from the function list rather than a new flag: declaring a second
    function under concurrencyControl is the intent, and a flag that had to
    agree with the list would be one more thing to contradict it.
    """
    return config.concurrency_control and len(config.functions) >= 2


def load_script_name(config: ScenarioConfig) -> str:
    """Which k6 script drives the function.

    `autoscaling.js` is not about autoscaling: it is the one that hammers a
    single function with whatever stages the plan supplies, which is exactly
    what a concurrency run needs. Selecting it by `config.autoscaling` alone
    silently gave the governor scenario `two-vm-function-invoke.js` instead,
    whose 100ms think time holds offered concurrency near 2 against a limit of
    8 — so the function was never concurrent, the governor had nothing to react
    to, and a think-time override aimed at the other script changed nothing.
    """
    if is_co_tenancy(config):
        # Staggered phases, which one global stage list cannot express, so these
        # scripts carry their own k6 scenarios.
        if config.load_profile == "burst":
            return "co-tenancy-burst.js"
        if config.load_profile == "openloop":
            return "open-loop.js"
        return "co-tenancy.js"
    if config.autoscaling or config.concurrency_control:
        return "autoscaling.js"
    return "two-vm-function-invoke.js"


def drain_checkpoints(drain_minutes: int) -> tuple[int, ...]:
    """Return the checkpoints to sample, in seconds, bounded by the drain window.

    The plan names 30 seconds, 5 minutes and 30 minutes, because those are the
    retention windows under test; a shorter drain simply stops earlier rather than
    silently sampling past its own end and reporting a window it never observed.
    """
    wanted = (0, 30, 300, 1800)
    limit = drain_minutes * 60
    inside = tuple(c for c in wanted if c <= limit)
    return inside if inside[-1] == limit else (*inside, limit)


def management_url_for(backend: str, control_plane_url: str) -> str | None:
    """Return where the control plane's own metrics are readable, or None.

    Only the container backend is wired here: its compose stack publishes the
    management port on the loopback address, so the observation needs no proxy. A
    Kubernetes run reaches the same endpoint through a port-forward that the drain
    observer does not own, and claiming an unproxied URL there would produce an
    unreachable sample rather than an honest absence.
    """
    if backend not in ("container", "containerd"):
        return None
    return control_plane_url.rsplit(":", 1)[0] + ":8081"


def k6_environment(
    config: ScenarioConfig, control_plane_url: str, function_name: str
) -> dict[str, str]:
    """Return what the load generator is told, including its concurrency.

    Measured, not assumed: with the script's default 50ms think time, a 180s
    phase at 25 VUs held a mean of 1.0 requests in flight against a limit of 8.
    A closed-loop VU keeps only S/(S+Z) of a request in flight, so for a 2.5ms
    function that think time caps the offered concurrency near 1 however many
    VUs are added — the function never became concurrent and the governor had
    nothing to react to. Zero think time makes in-flight equal the VU count.
    """
    env = {"NANOFAAS_URL": control_plane_url, "NANOFAAS_FUNCTION": function_name}
    if config.soak_minutes is not None:
        # Told in seconds because that is what a k6 stage takes; the scenario asks in
        # minutes because that is the unit the soak is reasoned about in.
        env["K6_SOAK_SECONDS"] = str(config.soak_minutes * 60)
    if config.concurrency_control:
        env["K6_THINK_SECONDS"] = "0"
        # The SLO as a caller would state it: a percentile of end-to-end
        # latency, not a mean of service time. The controller works from the mean
        # of what the function itself took, which is the right input for a
        # control loop and the wrong thing to promise anyone — a run measured a
        # mean inside its 10ms target while the tail reached 24ms.
        env["K6_MAX_P95_MS"] = str(end_to_end_p95_budget_ms(config))
    if config.load_profile == "saturation":
        # Shedding load is what this profile is for. Holding it to the ordinary
        # failure budget would mark every saturation run red for doing its job.
        env["K6_MAX_FAILED_RATE"] = "0.99"
        env.pop("K6_MAX_P95_MS", None)
    if config.load_profile == "openloop":
        # Arrivals scheduled by the clock, so the peak is a RATE rather than a
        # number of requests held open. Set from the measured saturation point —
        # about 1,400 served per second per function on these cores — so the peak
        # genuinely exceeds capacity and the queue has to grow, which is the
        # condition the closed loop could not create.
        env["K6_PEAK_RPS"] = str(_OPEN_LOOP_PEAK_RPS)
        env.pop("K6_MAX_P95_MS", None)
    if config.load_profile == "burst":
        # Closed-loop VUs each hold one request, in service or queued, so the peak
        # VU count IS the queue fill: depth = VUs - limit. Sitting just under
        # limit + queue means a controller brushes the top of the buffer and
        # refuses nothing, so a rejection is a verdict on the controller rather
        # than a property of how hard the generator was told to push.
        env["K6_PEAK_VUS"] = str(burst_peak_vus(config))
        env.pop("K6_MAX_P95_MS", None)
    if not math.isclose(config.load_scale, 1.0):
        # One knob for the whole shape: scaling the stages individually is how a
        # profile stops being the same profile.
        # Only the scale. The VU pool is sized by the script, which is where the
        # rates live: mirroring the peak here would put knowledge of one
        # experiment into the module every experiment shares.
        env["K6_RATE_SCALE"] = str(config.load_scale)
    if config.load_vus is not None:
        # Overrides the script's own sizing. Declared per run because the pool has
        # to be large enough that the platform, not the generator, is what gives
        # way - and how large that is cannot be known before the run.
        env["K6_MAX_VUS"] = str(config.load_vus)
    if is_co_tenancy(config):
        env["NANOFAAS_NEIGHBOUR"] = neighbour_name(config)
    return env


def burst_peak_vus(config: ScenarioConfig) -> int:
    """Offered concurrency at the top of a burst: three short of overflowing.

    Derived from the SMALLEST limit any mode under comparison will grant, not
    from the per-function ceiling. That was the flaw in the first run of this
    profile: the peak was calibrated against a ceiling of 8, while BUDGETED
    constrains the SUM, so under contention each function held about 4 and the
    load overflowed the queue by construction in one mode and not the other.
    The two runs were then not measuring the same thing.

    The same number has to be used for every mode or the runs stop being
    comparable, so the tighter constraint sets it for all of them.
    """
    limit = _CONCURRENCY_CEILING
    if is_co_tenancy(config):
        limit = min(limit, _BURST_TOTAL_BUDGET // len(config.functions))
    return limit + _CONCURRENCY_QUEUE_SIZE - 3


def neighbour_name(config: ScenarioConfig) -> str:
    """Return the function that comes and goes while the first one holds steady."""
    return config.functions[1]
