"""Indivisible Sonata measurement lifetime with ordered failure finalization."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Protocol, cast

from sonata_engine import Steps, Task, TaskInputs, TaskOutcome

from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.ports import Clock, WorkloadDriver


def phase_order() -> tuple[str, ...]:
    """Return the ordered phases of a complete soak run."""
    return (
        "build-preflight",
        "source-snapshot",
        "build-images",
        "publish-images",
        "freeze-digests",
        "deploy",
        "preflight",
        "prerequisites",
        "warmup",
        "baseline-drain",
        "baseline",
        "baseline-diagnostics",
        "steady",
        "drain",
        "final-diagnostics",
        "observer-stop",
        "evaluate",
        "report",
        "cleanup",
    )


class Sampling(Protocol):
    """Define the observer operations needed by the measurement lifetime."""

    def start(self, phase: str) -> None:
        """Start sampling in the initial phase."""
        ...

    def set_phase(self, phase: str) -> None:
        """Label subsequent samples with the current phase."""
        ...

    def stop(self, timeout_s: float) -> None:
        """Stop sampling within the supplied timeout."""
        ...


@dataclass
class LifecycleState:
    """Failure information passed to the report, never an implicit PASS receipt."""

    phase: str = "preflight"
    primary_error: BaseException | None = None
    secondary_errors: list[tuple[str, BaseException]] = field(default_factory=list)
    windows: dict[str, tuple[float, float]] = field(default_factory=dict)
    workload_receipts: dict[str, Path] = field(default_factory=dict)
    # Set only by a run whose protocol declares the hot switch: the step's own
    # receipt, published in the run root and referenced by the manifest, exactly
    # as the workload receipt is. A passing soak has to be able to evidence the
    # campaign's criterion from its own artifact.
    switch_receipt: Path | None = None
    evaluation: Any = None
    report: Path | None = None

    @property
    def aborted(self) -> bool:
        """Indicate whether the primary error represents an interrupted run."""
        return self.primary_error is not None and not isinstance(
            self.primary_error, Exception
        )


@dataclass(frozen=True)
class LifecycleHooks:
    """Bind preflight, prerequisites, diagnostics, evaluation and reporting."""

    preflight: Callable[[], None]
    prerequisites: Callable[[], None]
    baseline_capture: Callable[[LifecycleState, float], None]
    final_capture: Callable[[LifecycleState, float], None]
    evaluate: Callable[[LifecycleState], Any]
    report: Callable[[LifecycleState], Path]


@dataclass
class _Action(Task[Any]):
    title: str
    action: Callable[[], Any]

    def run(self, inputs: TaskInputs) -> TaskOutcome[Any]:
        return TaskOutcome(value=self.action())


class SoakLifecycle(Task[LifecycleState]):
    """Run via Workflow so every phase has Sonata events and journal identity.

    Hooks must enforce their I/O budgets. The platform owner must acquire all
    runtime bindings before constructing this measurement task. Setup failures
    before task entry remain the outer resource owner's responsibility.
    """

    title = "Run complete soak measurement"
    idempotent = False

    def __init__(
        self,
        config: SoakConfig,
        *,
        observer: Sampling,
        clock: Clock,
        driver_factory: Callable[[str], WorkloadDriver],
        hooks: LifecycleHooks,
        run_dir: Path,
        cancelled: Event | None = None,
        under_load: Callable[[float, Event], Any] | None = None,
    ) -> None:
        """Bind the policy and runtime adapters for one measurement lifetime."""
        self.config = config
        self.observer = observer
        self.clock = clock
        self.driver_factory = driver_factory
        self.hooks = hooks
        self.run_dir = run_dir
        self.cancelled = cancelled if cancelled is not None else Event()
        self.under_load = under_load
        self.state = LifecycleState()
        self._driver: WorkloadDriver | None = None
        self._entered = False
        self._observer_attempted = False
        self._observer_stopped = False

    def _check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise KeyboardInterrupt("soak cancelled; continuous run cannot resume")

    def _phase(self, name: str, action: Callable[[], Any]) -> Any:
        self._check_cancelled()
        self.state.phase = name
        started = self.clock.monotonic()
        try:
            return action()
        finally:
            self.state.windows[name] = (started, self.clock.monotonic())

    def _wait(self, duration: float) -> None:
        if not self.clock.wait_until(self.clock.monotonic() + duration, self.cancelled):
            raise KeyboardInterrupt("soak cancelled during natural observation")

    def _load(self, phase: str, duration: float) -> None:
        if phase == "warmup":
            self._observer_attempted = True
            self.observer.start("warmup")
        else:
            self.observer.set_phase(phase)
        self._driver = self.driver_factory(phase)
        under_load = self.under_load
        if phase == "steady" and under_load is not None:
            self._load_under_switching(self._driver, under_load, duration)
        else:
            self.state.workload_receipts[phase] = self._driver.run(
                self.run_dir / phase, duration, self.cancelled
            )
        self._stop_generator()
        self._check_cancelled()

    def _load_under_switching(
        self,
        driver: WorkloadDriver,
        under_load: Callable[[float, Event], Any],
        duration: float,
    ) -> None:
        """Run the load and the switch step together, and keep both receipts.

        The switch has to happen *under* the load, so the two cannot be
        sequenced. They are two threads rather than one interleaved loop because
        the switch is what this step is for: folding it into the workload driver
        would put the perturbation inside the thing it perturbs, and make the
        load's own receipt depend on the switch having succeeded.
        """
        with ThreadPoolExecutor(max_workers=2) as pool:
            load = pool.submit(
                driver.run, self.run_dir / "steady", duration, self.cancelled
            )
            switching = pool.submit(under_load, duration, self.cancelled)
            # Stop the other side as soon as either fails. Not a nicety: the
            # executor's own shutdown waits for the survivor, so without this a
            # switch that blows its budget one minute into a ninety-minute
            # steady phase would be reported ninety minutes later, with the
            # observer still sampling the process it is about to fail. An
            # operator meets that with Ctrl-C, which turns a FAIL into an ABORT
            # and loses the evidence - the worst outcome for the thing this step
            # exists to measure.
            done, _ = wait((load, switching), return_when=FIRST_EXCEPTION)
            first = next((f for f in done if f.exception() is not None), None)
            if first is not None:
                self.cancelled.set()
            wait((load, switching))
            self._keep_receipts(load, switching, first)

    def _keep_receipts(
        self,
        load: Future[Path],
        switching: Future[Path],
        first: Future[Path] | None,
    ) -> None:
        """Record both receipts, then surface the failure that caused the stop.

        Each receipt is recorded before anything is raised, so a switch that
        fails still leaves the load's receipt in the manifest: the load is the
        run's evidence, and losing it to the failure would leave the operator a
        verdict with nothing to read it against.

        The *first* failure is raised, not the loudest. When the load dies the
        switch then stops on the cancellation and reports a short count, which
        is a consequence of that death rather than a second, independent fault.
        """
        for future in (switching, load):
            try:
                receipt = future.result()
            except BaseException:
                continue
            if future is switching:
                self.state.switch_receipt = receipt
            else:
                self.state.workload_receipts["steady"] = receipt
        if first is not None:
            first.result()  # re-raises the failure that stopped the window

    def _stop_generator(self) -> None:
        if self._driver is not None:
            self._driver.stop(self.config.cancellation_timeout_s)
            self._driver = None

    def stop_observer(self) -> None:
        """Stop an attempted observer unless it has already stopped successfully."""
        if self._observer_attempted and not self._observer_stopped:
            self.observer.stop(self.config.cancellation_timeout_s)
            self._observer_stopped = True

    def _natural(self, phase: str, duration: float) -> None:
        self.observer.set_phase(phase)
        self._wait(duration)

    def run(self, inputs: TaskInputs) -> TaskOutcome[LifecycleState]:
        """Execute the measurement once and attempt every ordered finalizer."""
        if self._entered:
            raise RuntimeError("soak lifetime cannot be restarted or resumed")
        self._entered = True
        phases = self.config.phases
        actions = (
            ("preflight", self.hooks.preflight),
            ("prerequisites", self.hooks.prerequisites),
            ("warmup", lambda: self._load("warmup", phases.warmup_s)),
            (
                "baseline-drain",
                lambda: self._natural("baseline", phases.baseline_drain_s),
            ),
            ("baseline", lambda: self._natural("baseline", phases.baseline_window_s)),
            # Unconditional, so a run's phase keys do not depend on its policy;
            # the hook returns immediately when nothing is declared for the
            # baseline checkpoint. It runs after the baseline window closed, so
            # what it perturbs is outside the natural window it measures from.
            (
                "baseline-diagnostics",
                lambda: self.hooks.baseline_capture(
                    self.state, self.config.diagnostics.timeout_s
                ),
            ),
            ("steady", lambda: self._load("steady", phases.steady_s)),
            ("drain", lambda: self._natural("drain", phases.drain_s)),
        )
        try:
            Steps(
                title="Measure soak",
                steps=tuple(
                    _Action(
                        name, lambda name=name, action=action: self._phase(name, action)
                    )
                    for name, action in actions
                ),
            ).run(inputs)
        except BaseException as error:
            self.state.primary_error = error
        finally:
            finalizers = (
                ("stop-generator", self._stop_generator),
                (
                    "final-diagnostics",
                    lambda: self.hooks.final_capture(
                        self.state, self.config.diagnostics.timeout_s
                    ),
                ),
                ("observer-stop", self.stop_observer),
                (
                    "evaluate",
                    lambda: setattr(
                        self.state, "evaluation", self.hooks.evaluate(self.state)
                    ),
                ),
                (
                    "report",
                    lambda: setattr(
                        self.state, "report", self.hooks.report(self.state)
                    ),
                ),
            )
            for name, action in finalizers:
                try:
                    Steps(title=name, steps=(_Action(name, action),)).run(inputs)
                except BaseException as error:
                    if self.state.primary_error is None:
                        self.state.primary_error = error
                    else:
                        self.state.secondary_errors.append((name, error))
            if self.state.primary_error is not None:
                for name, error in self.state.secondary_errors:
                    self.state.primary_error.add_note(
                        f"{name}: {type(error).__name__}: {error}"
                    )
                raise self.state.primary_error
        return TaskOutcome(value=self.state)


def _publish_run_document(run_dir: Path, name: str, payload: dict[str, Any]) -> Path:
    """Publish beside Sonata's journal without acquiring the nonempty run root."""
    import json
    import os
    import tempfile

    if run_dir.is_symlink():
        raise ValueError("run root cannot be a symbolic link")
    run_dir.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(body) > 1024 * 1024:
        raise ValueError("run document exceeds its byte budget")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".soak-pending-", dir=run_dir)
    temporary = Path(temporary_name)
    target = run_dir / name
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def write_terminal_receipt(
    run_dir: Path,
    status: str,
    *,
    report_path: Path | None = None,
    reason: str | None = None,
) -> Path:
    """Freeze the CLI contract once; never replace an earlier terminal verdict."""
    if status not in {"PASS", "FAIL", "INCONCLUSIVE", "ABORTED"}:
        raise ValueError("unsupported soak terminal status")
    payload: dict[str, Any] = {"schema": "nanolab-soak-v1", "status": status}
    if report_path is not None:
        payload["report_path"] = str(report_path)
    if reason is not None:
        payload["reason"] = reason[:4096]
    return _publish_run_document(run_dir, "terminal.json", payload)


def write_policy_input(run_dir: Path, scenario: Any) -> Path | None:
    """Persist the loader's policy receipt and fingerprint the resolved policy."""
    from nanolab.tasks.soak.artifacts import fingerprint

    receipt = getattr(scenario, "_soak_policy_receipt", None)
    if receipt is None:
        return None
    return _publish_run_document(
        run_dir,
        "policy-input.json",
        {
            "schema": "nanolab-soak-v1",
            "policy_receipt": dict(receipt),
            "configuration_fingerprint": fingerprint(scenario.model_dump(mode="json")),
        },
    )


class TerminalSoakLifecycle(SoakLifecycle):
    """Persist the terminal contract before surfacing non-PASS to Sonata/TUI.

    The evaluate hook must return the acceptance gate's dictionary containing
    status. A missing or unsupported status is INCONCLUSIVE, never success.
    """

    def run(self, inputs: TaskInputs) -> TaskOutcome[LifecycleState]:
        """Persist the terminal verdict and surface failure to the workflow."""
        error: BaseException | None = None
        try:
            super().run(inputs)
        except BaseException as caught:
            error = caught
        evaluation = self.state.evaluation
        status = evaluation.get("status") if isinstance(evaluation, dict) else None
        if status not in {"PASS", "FAIL", "INCONCLUSIVE", "ABORTED"}:
            status = "INCONCLUSIVE"
        if error is not None:
            if not isinstance(error, Exception):
                status = "ABORTED"
            elif status == "PASS":
                status = "INCONCLUSIVE"
        try:
            write_terminal_receipt(
                self.run_dir,
                status,
                report_path=self.state.report,
                reason=str(error) if error is not None else None,
            )
        except BaseException as terminal_error:
            if error is not None:
                error.add_note(f"terminal receipt unavailable: {terminal_error}")
                raise error from terminal_error
            raise
        if status == "ABORTED":
            raise KeyboardInterrupt(
                "ABORTED: soak interrupted; see terminal.json"
            ) from error
        if status != "PASS":
            raise RuntimeError(
                f"{status}: soak did not pass; see terminal.json"
            ) from error
        return TaskOutcome(value=self.state)


def observer_startup_timeout(config: SoakConfig, *, overhead_s: float = 5.0) -> float:
    """Budget the observer's first sequential sweep of every configured role."""
    import math

    if isinstance(overhead_s, bool) or not math.isfinite(overhead_s) or overhead_s <= 0:
        raise ValueError("observer startup overhead must be finite and positive")
    return len(config.roles) * config.scrape_timeout_s + overhead_s


def make_workload_driver_factory(
    config: SoakConfig,
    *,
    base_url: str,
    payloads: dict[str, list[dict[str, object]]],
    image_digests: dict[str, str],
    command: tuple[str, ...] = ("k6",),
    request_timeout_s: float = 30.0,
) -> Callable[[str], WorkloadDriver]:
    """Bind task 7 without spawning a generator or reading its script at plan time.

    Preserve fractional arrival rates and both VU bounds. Freeze the artifact
    budget with the rest of the policy before either workload phase starts.
    """
    from copy import deepcopy

    rates = config.workload.rates
    if set(payloads) != set(rates) or set(image_digests) != set(config.roles):
        raise ValueError(
            "workload payloads and frozen images must cover every configured role"
        )
    frozen_payloads = deepcopy(payloads)
    frozen_images = dict(image_digests)
    frozen_config = config.model_dump(mode="json")
    frozen_rates = dict(rates)
    vus = config.workload.preallocated_vus
    max_vus = config.workload.max_vus
    artifact_limit = config.artifact_limit_bytes
    stop_timeout = config.cancellation_timeout_s

    def create(phase: str) -> WorkloadDriver:
        from nanolab.tasks.soak.workload import K6WorkloadDriver

        if phase not in {"warmup", "steady"}:
            raise ValueError("workload may run only during warmup or steady")
        return K6WorkloadDriver(
            base_url=base_url,
            function_rates=frozen_rates,
            payloads=frozen_payloads,
            image_digests=frozen_images,
            config=frozen_config,
            vus=vus,
            command=command,
            max_vus=max_vus,
            artifact_limit_bytes=artifact_limit,
            graceful_stop_s=stop_timeout,
            request_timeout_s=request_timeout_s,
        )

    return create


def run_prerequisite_gate(
    config: SoakConfig,
    *,
    inputs: dict[str, object],
    runner: Any,
    run_dir: Path,
    receipt_root: Path,
    timeout_s: float,
) -> dict[str, object]:
    """Bind task 8 in a synchronous Sonata hook, preserving its own receipt schema.

    A live runner is mandatory in run mode. It must own a fresh platform for each
    coverage ID and observe actual retained populations. This function never
    substitutes the measured platform or inferred HTTP success for that runner.
    """
    import asyncio
    import json
    from copy import deepcopy

    from nanolab.tasks.soak.artifacts import ArtifactWriter, fingerprint
    from nanolab.tasks.soak.prerequisites import run_prerequisites, validate_receipt

    coverage = frozenset(config.prerequisites.required_coverage)
    if not coverage:
        if config.purpose != "smoke":
            raise ValueError("P24 cannot omit prerequisite coverage")
        return {
            "schema": "nanolab-soak-v1",
            "kind": "prerequisite-exemption",
            "purpose": "smoke",
            "p24_qualified": False,
            "coverage": [],
        }
    if config.prerequisites.mode == "run":
        if runner is None:
            raise RuntimeError(
                "INCONCLUSIVE: live prerequisite runner unavailable; isolated resource "
                "ownership, cancellation-safe release and retained-population "
                "sources required"
            )
        writer = ArtifactWriter(run_dir / "prerequisites", config.artifact_limit_bytes)
        try:
            receipt = asyncio.run(
                run_prerequisites(
                    inputs=inputs,
                    required_coverage=coverage,
                    runner=runner,
                    writer=writer,
                    timeout_s=timeout_s,
                )
            )
        finally:
            writer.close()
        if receipt.get("status") != "PASS":
            raise RuntimeError(
                f"{receipt.get('status', 'INCONCLUSIVE')}: prerequisites did not pass"
            )
        return receipt

    results = []
    for name in sorted(coverage):
        path = Path(config.prerequisites.receipts[name])
        if not path.is_absolute():
            path = receipt_root / path
        if path.is_symlink():
            raise ValueError("prerequisite receipt cannot be a symbolic link")
        with path.open("rb") as stream:
            body = stream.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("prerequisite receipt exceeds its byte budget")
        receipt = json.loads(body)
        declared = receipt.get("coverage") if isinstance(receipt, dict) else None
        selected = (
            coverage
            if isinstance(declared, list) and set(declared) == coverage
            else frozenset({name})
        )
        expected = deepcopy(inputs)
        relevant = cast("dict[str, Any]", expected["relevant_config"])
        expected["relevant_config"] = {key: relevant[key] for key in selected}
        result = validate_receipt(receipt, fingerprint(expected), selected)
        results.append(result)
        if result.status != "PASS":
            raise RuntimeError(f"{result.status}: prerequisite {name}: {result.reason}")
    return {
        "schema": "nanolab-soak-v1",
        "kind": "prerequisite-gate",
        "status": "PASS",
        "coverage": sorted(coverage),
        "evidence": [path for result in results for path in result.evidence],
    }
