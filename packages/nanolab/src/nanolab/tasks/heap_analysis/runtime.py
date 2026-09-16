"""The ordered control-plane heap-analysis lifecycle.

Two post-full-GC control-plane heap dumps around a bounded k6 workload, then
headless MAT over both once the deployment is gone. The order is a safety
contract: the natural drain reading is taken BEFORE the final diagnostic GC,
because an unperturbed RSS/cgroup checkpoint is the whole point of that step,
and MAT is a separate Sonata task that declares none of the deployment's
resources, so the compiler releases the deployment before analysis starts.

This is a diagnostic, not a verdict. It never calls `create_soak_lifecycle`,
carries no P24 criteria and no retention gates, and `PASS` means only that the
receipt is complete.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Any, Literal, Protocol

from sonata_engine import Task, TaskInputs, TaskOutcome, Workflow
from sonata_engine.journal import JournalConfig

from nanolab.config.heap_analysis import HeapAnalysisConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.config.soak import (
    DiagnosticPolicy,
    PhaseConfig,
    PrerequisitePolicy,
    SoakConfig,
)
from nanolab.tasks.heap_analysis.evidence import native_comparison, persist_native
from nanolab.tasks.heap_analysis.mat import MatAnalysisRequest, MatAnalyzer
from nanolab.tasks.soak.artifacts import (
    ArtifactLimitExceededError,
    ArtifactWriter,
    describe_artifact,
    enforce_limit,
    measure_tree,
)
from nanolab.tasks.soak.diagnostic_helper import (
    GC_SOURCES,
    DockerHelperSpec,
    LocalDockerDiagnosticProvisioner,
)
from nanolab.tasks.soak.diagnostics import DiagnosticBudget
from nanolab.tasks.soak.helper_build import HelperImageRequest, build_helper_image
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.preparation import PreparationOptions, PreparedSoak
from nanolab.tasks.soak.workflow import (
    _publish_run_document,
    make_workload_driver_factory,
    write_terminal_receipt,
)

Status = Literal["PASS", "FAIL", "INCONCLUSIVE"]

REPORT_SCHEMA = "nanolab-heap-analysis-report-v1"

# The sentence the design requires the terminal receipt to carry, so nobody
# reading report.json mistakes a complete diagnostic for a verdict on memory.
PASS_MEANING = (  # nosec B105 - prose explaining the PASS status, not a credential
    "PASS means the diagnostic receipt is complete: the workload finished "
    "without request failures, both verified post-full-GC control-plane dumps "
    "were captured, and every required MAT report was produced. It is not a "
    "no-leak verdict and it is not P24 qualification."
)

_HELPER_DIGEST = re.compile(r"[^\s@]+@sha256:[a-f0-9]{64}")

# A diagnostic protocol takes three discrete checkpoints instead of a sampled
# time series, and has no baseline acceptance window. These values exist only
# to satisfy SoakConfig's internal consistency rules for the deployment it
# describes; nothing in this lifecycle reads them.
_INERT_SAMPLE_INTERVAL_S = 5
_INERT_SCRAPE_TIMEOUT_S = 5
_INERT_OBSERVATION_GAP_S = 15
_INERT_BASELINE_S = 10

# checkpoint -> the phase its natural checkpoint document declares. The
# adapter still requires an exact match, so a baseline capture can never be
# satisfied by a drain checkpoint, or the other way round.
_CHECKPOINT_PHASES = {"before-baseline": "baseline", "natural-drain": "drain"}
_CAPTURE_PHASES = {"baseline": "baseline", "final": "drain"}


@dataclass(frozen=True)
class HeapAnalysisResult:
    """The terminal diagnostic receipt this workflow exists to produce."""

    status: Status
    report: Path
    baseline_dump: Path | None
    final_dump: Path | None
    analysis_manifest: Path | None
    reasons: tuple[str, ...] = ()


class HeapAnalysisSession(Protocol):
    """The owned, side-effecting half of one run, bound to a live deployment.

    Every method here perturbs or observes real processes, so the ordered
    lifecycle drives this protocol and tests substitute it wholesale.
    """

    def load(self, phase: str, duration_s: float) -> Path:
        """Run the bounded generator for one phase; return its receipt path."""
        ...

    def settle(self, duration_s: float) -> None:
        """Wait out the unperturbed natural drain, touching nothing."""
        ...

    def observe(self, checkpoint: str) -> Path:
        """Record runtime/memory evidence and publish its natural checkpoint."""
        ...

    def full_gc(self, checkpoint: str) -> Path:
        """Request one full GC and verify its completion evidence."""
        ...

    def heap_dump(self, checkpoint: str) -> Path:
        """Capture one post-GC control-plane HPROF; return the dump path."""
        ...

    def stop_load(self, timeout_s: float) -> None:
        """Stop the generator when the run is cut short. Idempotent."""
        ...

    def close(self) -> None:
        """Remove helper containers and temporary volumes. Idempotent."""
        ...


@dataclass(frozen=True)
class HeapAnalysisWiring:
    """One run's bound collaborators: evidence, session, deployment, analysis."""

    writer: ArtifactWriter
    session: HeapAnalysisSession
    compose: Callable[[Task[Any]], Workflow]
    analyze: Callable[[MatAnalysisRequest], Path]
    helper_image: str


@dataclass(frozen=True)
class HeapAnalysisOptions:
    """Operational adapters, separate from the frozen diagnostic protocol."""

    preparation: PreparationOptions = field(default_factory=PreparationOptions)
    prepared: PreparedSoak | None = None
    docker_socket: str = "/var/run/docker.sock"
    helper_builder: str = "nanolab-heap-analysis"
    # An already-published helper digest, which skips this run's build. The
    # build is the default: a digest only names anything in the registry that
    # holds it, so supply one only when it is already there.
    helper_image: str | None = None
    # Replaces the whole Docker-bound half of a run: preparation, deployment
    # composition, the diagnostic session and MAT.
    wiring: Callable[[Path], HeapAnalysisWiring] | None = None


def deployment_protocol(config: HeapAnalysisConfig, helper_image: str) -> SoakConfig:
    """Describe the deployment heap analysis needs, with no acceptance policy.

    `purpose="diagnostic"` states in the type system what this protocol is: it
    deploys and observes but reaches no verdict, so it declares no criteria and
    no retention gates. Only this function builds one.

    `helper_image` is the digest this run's helper build published; it is not a
    scenario constant, because a digest naming bytes in one machine's registry
    is unusable anywhere else.
    """
    if _HELPER_DIGEST.fullmatch(helper_image) is None:
        raise ValueError("helper_image must be digest-pinned")
    return SoakConfig(
        purpose="diagnostic",
        phases=PhaseConfig(
            warmup_s=config.warmup_s,
            baseline_drain_s=_INERT_BASELINE_S,
            baseline_window_s=_INERT_BASELINE_S,
            steady_s=config.steady_s,
            drain_s=config.drain_s,
            cleanup_margin_s=_INERT_BASELINE_S,
        ),
        roles=config.roles,
        images=config.images,
        diagnostics=DiagnosticPolicy(
            operations={"control-plane": ["gc", "heap_dump"]},
            timeout_s=config.diagnostic_timeout_s,
            max_dumps=config.max_dumps,
            max_dump_bytes=config.max_dump_bytes,
            helper_images={"control-plane": helper_image},
            gc_completion_evidence={"control-plane": GC_SOURCES["jvm"]},
        ),
        prerequisites=PrerequisitePolicy(required_coverage=[], relevant_config_keys={}),
        sample_interval_s=_INERT_SAMPLE_INTERVAL_S,
        scrape_timeout_s=_INERT_SCRAPE_TIMEOUT_S,
        max_observation_gap_s=_INERT_OBSERVATION_GAP_S,
        artifact_limit_bytes=config.artifact_limit_bytes,
        cancellation_timeout_s=config.diagnostic_timeout_s,
        workload=config.workload,
    )


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        body = stream.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError(f"document exceeds its byte budget: {path}")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError(f"document must be a JSON object: {path}")
    return value


def _counter(receipt: dict[str, Any], name: str) -> int | None:
    counter = receipt.get("counters", {}).get(name, {})
    if not isinstance(counter, dict) or counter.get("availability") != "observed":
        return None
    value = counter.get("value")
    return value if type(value) is int else None


def workload_outcome(receipt_path: Path) -> tuple[Status, tuple[str, ...]]:
    """Judge only the workload's own success contract, never memory behaviour.

    A definite, observed violation is FAIL. A receipt that cannot be read, or
    that never completed, leaves the diagnostic incomplete, not failed.
    """
    try:
        receipt = _read_json(receipt_path)
    except (OSError, ValueError) as error:
        return "INCONCLUSIVE", (f"workload receipt unreadable: {error}",)
    if receipt.get("completed") is not True:
        return "INCONCLUSIVE", ("workload did not complete",)
    errors = _counter(receipt, "error")
    dropped = _counter(receipt, "dropped")
    if errors is None or dropped is None:
        return "INCONCLUSIVE", ("workload request counters were not observed",)
    reasons = []
    if errors:
        reasons.append(f"workload recorded {errors} request failures")
    if dropped:
        reasons.append(f"workload dropped {dropped} scheduled iterations")
    if receipt.get("threshold_failed") is True:
        reasons.append("workload breached its declared thresholds")
    declared = receipt.get("errors")
    if isinstance(declared, list) and declared:
        reasons.extend(str(item)[:256] for item in declared[:8])
    return ("FAIL", tuple(reasons)) if reasons else ("PASS", ())


@dataclass(frozen=True)
class _Measurement:
    """What the measured half hands to analysis: dumps and a workload verdict."""

    baseline_dump: Path
    final_dump: Path
    workload_receipt: Path
    workload_status: Status
    reasons: tuple[str, ...]


class _MeasureControlPlaneHeap(Task[_Measurement]):
    """Drive the ordered measurement while the deployment resources are held."""

    title = "Measure the control-plane heap around a bounded workload"
    idempotent = False
    # compose_frozen_soak_workflow owns a measurement observer resource. Heap
    # analysis takes three discrete checkpoints instead of a sampled series, so
    # there is no background observer to hold or stop.
    observer = None

    def __init__(
        self,
        config: HeapAnalysisConfig,
        session: HeapAnalysisSession,
        holder: dict[str, Any],
    ) -> None:
        """Bind the protocol and its session without touching either yet."""
        self.config, self.session, self.holder = config, session, holder

    def stop_observer(self) -> None:
        """Satisfy the composed observer resource; nothing runs in background."""

    def run(self, inputs: TaskInputs) -> TaskOutcome[_Measurement]:
        """Execute the safety order exactly once, or fail the whole workflow."""
        config, session = self.config, self.session
        try:
            session.load("warmup", config.warmup_s)
            session.observe("before-baseline")
            session.full_gc("baseline")
            baseline = session.heap_dump("baseline")
            receipt = session.load("steady", config.steady_s)
            session.settle(config.drain_s)
            # Unperturbed, and deliberately before the final diagnostic GC:
            # collecting first would destroy the reading this step exists for.
            session.observe("natural-drain")
            session.full_gc("final")
            session.observe("after-final-gc")
            final = session.heap_dump("final")
        except BaseException as error:
            try:
                session.stop_load(config.diagnostic_timeout_s)
            except BaseException as stop_error:
                error.add_note(f"generator stop unconfirmed: {stop_error}")
            raise
        status, reasons = workload_outcome(receipt)
        measurement = _Measurement(baseline, final, receipt, status, reasons)
        self.holder["measurement"] = measurement
        return TaskOutcome(value=measurement)


class _AnalyzeHeapDumps(Task[Path]):
    """Run MAT over the released dumps; declares no deployment resource."""

    title = "Analyze the released control-plane heap dumps"
    idempotent = False

    def __init__(
        self,
        config: HeapAnalysisConfig,
        analyze: Callable[[MatAnalysisRequest], Path],
        holder: dict[str, Any],
        run_dir: Path,
        helper_image: str,
    ) -> None:
        """Bind the analysis inputs; MAT itself starts only inside run()."""
        self.config, self.analyze = config, analyze
        self.holder, self.run_dir = holder, run_dir
        self.helper_image = helper_image

    def run(self, inputs: TaskInputs) -> TaskOutcome[Path]:
        """Consume the measurement result, never a deployment resource."""
        measurement = self.holder["measurement"]
        config = self.config
        request = MatAnalysisRequest(
            baseline_hprof=measurement.baseline_dump,
            final_hprof=measurement.final_dump,
            output_dir=self.run_dir.absolute(),
            helper_image=self.helper_image,
            artifact_limit_bytes=config.artifact_limit_bytes,
            mat_memory_mib=config.mat_memory_mib,
            mat_cpus=config.mat_cpus,
            mat_timeout_s=config.mat_timeout_s,
            uid=os.getuid(),
            gid=os.getgid(),
        )
        manifest = self.analyze(request)
        self.holder["manifest"] = manifest
        return TaskOutcome(value=manifest)


def _analysis_status(manifest: Path | None) -> tuple[Status, tuple[str, ...]]:
    if manifest is None:
        return "INCONCLUSIVE", ("MAT analysis did not produce a manifest",)
    try:
        status = _read_json(Path(manifest)).get("status")
    except (OSError, ValueError) as error:
        return "INCONCLUSIVE", (f"MAT manifest unreadable: {error}",)
    if status != "PASS":
        return "INCONCLUSIVE", (f"MAT analysis is incomplete: {status}",)
    return "PASS", ()


def _combine(*outcomes: tuple[Status, tuple[str, ...]]) -> tuple[Status, list[str]]:
    """Keep soak's precedence: a definite failure outranks absent evidence."""
    reasons = [reason for _status, items in outcomes for reason in items]
    statuses = {status for status, _items in outcomes}
    if "FAIL" in statuses:
        return "FAIL", reasons
    if "INCONCLUSIVE" in statuses:
        return "INCONCLUSIVE", reasons
    return "PASS", reasons


class LocalHeapAnalysisSession:
    """The real owned session: k6, the JVM helper, and observed evidence.

    Nothing here runs at construction. The diagnostic helper is provisioned on
    first use, which is after the deployment is up, and removed by `close()`
    on every terminal path.
    """

    def __init__(
        self,
        config: HeapAnalysisConfig,
        prepared: PreparedSoak,
        deployment: Any,
        *,
        helper_image: str,
        docker_socket: str = "/var/run/docker.sock",
        generator_command: tuple[str, ...] = ("k6",),
    ) -> None:
        """Freeze the run's inputs without discovering or provisioning anything."""
        self._config = config
        self._helper_image = helper_image
        self._prepared = prepared
        self._deployment = deployment
        self._root = prepared.evidence_dir
        self._writer = prepared.writer
        self._docker_socket = docker_socket
        self._cancelled = Event()
        self._drivers = make_workload_driver_factory(
            deployment_protocol(config, helper_image),
            base_url=deployment.api_endpoint,
            payloads=prepared.payloads,
            image_digests=prepared.images,
            command=generator_command,
        )
        self._driver: Any = None
        self._helper: Any = None
        self._target: Target | None = None
        self._adapters: dict[str, Any] = {}
        self._closed = False

    def _decision(self) -> dict[str, Any]:
        inputs = getattr(self._deployment, "diagnostic_inputs", None) or {}
        decision = inputs.get("roles", {}).get("control-plane")
        if decision is None:
            raise RuntimeError(
                "the owned deployment carries no control-plane diagnostic inputs"
            )
        return decision

    def _bind(self) -> tuple[Target, Any]:
        """Discover the live control plane and provision its helper once."""
        if self._helper is not None and self._target is not None:
            return self._target, self._helper
        config, decision = self._config, self._decision()
        targets = self._deployment.discover()
        target = next((item for item in targets if item.role == "control-plane"), None)
        if target is None or target.runtime != "jvm":
            raise RuntimeError("no running JVM control plane to analyze")
        # Must contain every checkpoint's capture directory (`_capture` writes
        # to `self._root / f"{checkpoint}-{operation}"`, not under a
        # "diagnostics" subdir): the executor's containment check
        # (diagnostic_exec.py's `execute`) requires each capture's output_dir
        # to resolve inside this root, matching soak's own convention of
        # passing its evidence root directly (see `diagnostic_root=root` in
        # nanolab.tasks.soak.runtime).
        output_root = self._root.absolute()
        provisioner = LocalDockerDiagnosticProvisioner(
            assets_dir=Path(__file__).resolve().parents[4] / "assets/soak",
            cancelled=self._cancelled,
        )
        helper = provisioner.prepare(
            DockerHelperSpec(
                target=target,
                helper_image=decision["helper_image"],
                owner_label="com.docker.compose.project",
                owner_value=self._deployment.project.name,
                uid=decision["uid"],
                gid=decision["gid"],
                output_root=output_root,
                quota_bytes=decision["quota_bytes"],
                helper_memory_bytes=decision["helper_memory_bytes"],
                allow_target_stop_on_cancel=True,
                docker_host="unix://" + self._docker_socket,
                architecture=config.images["control-plane"].platform.split("/")[1],
                target_tmp_volume=decision["target_tmp_volume"],
            ),
            timeout_s=min(config.diagnostic_timeout_s, 300),
        )
        # config.max_dump_bytes is the single-dump quota ceiling (see the
        # helper's 1 GiB volume contract in _diagnostic_resource_inputs), not
        # a total across every dump this session takes from this one role
        # (baseline, then final). Size the shared budget for max_dumps
        # reservations of the derived per-dump quota, or the first dump
        # exhausts it and the second always fails.
        budget = DiagnosticBudget(
            config.max_dumps,
            config.max_dumps * decision["quota_bytes"],
            self._deployment.diagnostic_inputs["available_artifact_bytes"],
        )
        self._adapters = {
            checkpoint: helper.adapter(
                budget=budget,
                natural_checkpoint=self._root / f"natural-{phase}.json",
                max_capture_bytes=decision["quota_bytes"],
                natural_phase=phase,
            )
            for checkpoint, phase in _CAPTURE_PHASES.items()
        }
        self._helper, self._target = helper, target
        return target, helper

    def load(self, phase: str, duration_s: float) -> Path:
        """Run one bounded k6 phase into its own evidence directory."""
        self._driver = self._drivers(phase)
        return self._driver.run(self._root / phase, duration_s, self._cancelled)

    def settle(self, duration_s: float) -> None:
        """Wait out the natural drain, interruptibly and without perturbation."""
        if self._cancelled.wait(duration_s):
            raise KeyboardInterrupt("heap analysis cancelled during natural drain")

    def observe(self, checkpoint: str) -> Path:
        """Record the optional readings and seal the natural checkpoint."""
        targets = self._deployment.discover()
        observed = self._deployment.observations(targets)
        _target, helper = self._bind()
        readings = helper.read_memory(
            timeout_s=min(self._config.diagnostic_timeout_s, 300),
            include_smaps=True,
            include_heap_info=True,
        )
        native = persist_native(
            self._root,
            checkpoint,
            readings,
            self._config.artifact_limit_bytes,
        )
        document = {
            "schema": "nanolab-soak-v1",
            "kind": "runtime_observation",
            "checkpoint": checkpoint,
            "observed": observed,
            "native": native,
        }
        # Raw files are outside ArtifactWriter's individual-record accounting.
        # Check the cumulative run budget before and after the JSON write too.
        required = len(json.dumps(document).encode("utf-8")) + 1
        if (
            measure_tree(self._root.parent) + required + 4096
            > self._config.artifact_limit_bytes
        ):
            raise ArtifactLimitExceededError(
                "runtime observation exceeds cumulative artifact budget"
            )
        evidence = self._writer.write_json(f"runtime-{checkpoint}.json", document)
        enforce_limit(self._root.parent, self._config.artifact_limit_bytes)
        phase = _CHECKPOINT_PHASES.get(checkpoint)
        if phase is not None:
            target, _helper = self._bind()
            self._writer.write_json(
                f"natural-{phase}.json",
                {
                    "schema": "nanolab-soak-v1",
                    "kind": "natural_checkpoint",
                    "phase": phase,
                    "completed": True,
                    "target": asdict(target),
                    "ended_s": time.monotonic(),
                    "artifacts": [
                        {**describe_artifact(evidence), "path": evidence.name}
                    ],
                },
            )
        return evidence

    def _capture(self, checkpoint: str, operation: str) -> Path:
        from nanolab.tasks.soak.runtime import _capture_owned_diagnostic

        target, helper = self._bind()
        output = self._root / f"{checkpoint}-{operation}"
        output.mkdir(mode=0o700)
        return _capture_owned_diagnostic(
            self._adapters[checkpoint],
            helper,
            target,
            operation,
            output,
            self._config.diagnostic_timeout_s,
            self._cancelled,
        )

    def full_gc(self, checkpoint: str) -> Path:
        """Request a full GC whose completion evidence the adapter verifies."""
        return self._capture(checkpoint, "gc")

    def heap_dump(self, checkpoint: str) -> Path:
        """Capture one post-GC HPROF and return the dump the adapter wrote."""
        receipt = self._capture(checkpoint, "heap_dump")
        dump = receipt.parent / "artifacts" / "capture.hprof"
        if not dump.is_file() or not dump.stat().st_size:
            raise RuntimeError(f"no heap dump was produced for {checkpoint}")
        return dump

    def stop_load(self, timeout_s: float) -> None:
        """Cancel the run and stop the generator; safe to call more than once."""
        self._cancelled.set()
        if self._driver is not None:
            self._driver.stop(timeout_s)

    def close(self) -> None:
        """Remove the helper container and its temporary volume, exactly once."""
        if self._closed:
            return
        self._closed = True
        if self._helper is not None:
            self._helper.close()


class RunControlPlaneHeapAnalysis(Task[HeapAnalysisResult]):
    """An indivisible deferred workflow; every terminal path cleans up."""

    title = "Capture and analyze control-plane heap dumps"
    idempotent = False

    def __init__(
        self,
        config: ScenarioConfig,
        bindings: Any,
        *,
        run_dir: Path,
        repo_root: Path,
        tool_root: Path,
        options: HeapAnalysisOptions | None = None,
    ) -> None:
        """Freeze execution inputs without acquiring files, processes or images."""
        if config.workflow != "heap-analysis" or config.heap_analysis is None:
            raise ValueError("heap analysis requires a heap-analysis scenario")
        self.config, self.bindings = config, bindings
        self.run_dir, self.repo_root, self.tool_root = run_dir, repo_root, tool_root
        self.options = options or HeapAnalysisOptions()
        self.protocol: HeapAnalysisConfig = config.heap_analysis
        self._entered = False

    def run(self, inputs: TaskInputs) -> TaskOutcome[HeapAnalysisResult]:
        """Deploy, measure, release, analyze, clean up, and publish the receipt."""
        if self._entered:
            raise RuntimeError("heap analysis cannot resume or restart")
        self._entered = True
        holder: dict[str, Any] = {}
        wiring: HeapAnalysisWiring | None = None
        reasons: list[str] = []
        interrupted: BaseException | None = None
        try:
            factory = self.options.wiring or self._local_wiring
            wiring = factory(self.run_dir)
            self._execute(wiring, holder)
        except Exception as error:
            reasons.append(f"{type(error).__name__}: {str(error)[:1024]}")
        except BaseException as error:
            interrupted = error
            reasons.append(f"{type(error).__name__}: {str(error)[:1024]}")
        finally:
            if wiring is not None:
                try:
                    wiring.session.close()
                except BaseException as cleanup_error:
                    reasons.append(f"helper cleanup unconfirmed: {cleanup_error}")
        if wiring is None:
            write_terminal_receipt(
                self.run_dir, "INCONCLUSIVE", reason="; ".join(reasons)
            )
            raise RuntimeError("; ".join(reasons) or "heap analysis could not start")
        result = self._publish(wiring, holder, reasons, aborted=interrupted is not None)
        if interrupted is not None:
            raise interrupted
        return TaskOutcome(value=result)

    def _execute(self, wiring: HeapAnalysisWiring, holder: dict[str, Any]) -> None:
        """Compose the deployment around measurement, with MAT outside it."""
        measurement = _MeasureControlPlaneHeap(self.protocol, wiring.session, holder)
        workflow = wiring.compose(measurement)
        # The resource boundary: this task declares no deployment resource, so
        # Workflow.compile() — which splices each resource's release unit after
        # its LAST consumer, the measurement — places every deployment release
        # ahead of it. MAT cannot start while the deployment is held.
        workflow.add(
            _AnalyzeHeapDumps(
                self.protocol,
                wiring.analyze,
                holder,
                self.run_dir,
                wiring.helper_image,
            )
        )
        workflow.run(
            journal=JournalConfig(path=self.run_dir / "heap-analysis-journal.jsonl")
        )

    def _publish(
        self,
        wiring: HeapAnalysisWiring,
        holder: dict[str, Any],
        reasons: list[str],
        *,
        aborted: bool,
    ) -> HeapAnalysisResult:
        """Publish report.json at the run root, then the frozen CLI receipt.

        The design's stable artifact set puts report.json beside the run's
        journal, not inside evidence/, so it uses the same run-root publisher
        terminal.json does. The evidence/ tree keeps its bounded writer and its
        budget untouched; the writer is closed here because this is the last
        thing the run writes - in a `finally`, together with terminal.json, so
        that a failed report.json still leaves the CLI something to read
        instead of an unclosed writer and no terminal verdict.
        """
        measurement: _Measurement | None = holder.get("measurement")
        manifest = holder.get("manifest")
        workload: tuple[Status, tuple[str, ...]] = (
            ("INCONCLUSIVE", ("the measured workload did not complete",))
            if measurement is None
            else (measurement.workload_status, measurement.reasons)
        )
        status, combined = _combine(workload, _analysis_status(manifest))
        combined.extend(reasons)
        if reasons and status == "PASS":
            status = "INCONCLUSIVE"
        baseline = None if measurement is None else measurement.baseline_dump
        final = None if measurement is None else measurement.final_dump
        dumps = {
            name: path
            for name, path in (("baseline", baseline), ("final", final))
            if path is not None and path.is_file()
        }
        report: Path | None = None
        try:
            report = _publish_run_document(
                self.run_dir,
                "report.json",
                {
                    "schema": REPORT_SCHEMA,
                    "status": status,
                    "meaning": PASS_MEANING,
                    # HPROF files and MAT reports can hold request payloads and
                    # other captured values, so they are referenced, never printed.
                    "sensitive": True,
                    "scope": "diagnostic-only",
                    "target": self.protocol.target,
                    "workload_status": workload[0],
                    "reasons": combined,
                    "dumps": {
                        name: describe_artifact(Path(path))
                        for name, path in dumps.items()
                    },
                    "native": native_comparison(wiring.writer.root),
                    "analysis_manifest": None if manifest is None else str(manifest),
                    "workload_receipt": (
                        None
                        if measurement is None
                        else str(measurement.workload_receipt)
                    ),
                },
            )
        finally:
            try:
                wiring.writer.close()
            except Exception as error:  # evidence close must not lose the receipt
                combined.append(f"evidence writer close failed: {error}")
            write_terminal_receipt(
                self.run_dir,
                "ABORTED"
                if aborted
                else (status if report is not None else "INCONCLUSIVE"),
                report_path=report,
                reason="; ".join(combined) or None,
            )
        if report is None:
            raise RuntimeError("report.json was not published")
        return HeapAnalysisResult(
            status=status,
            report=report,
            baseline_dump=baseline,
            final_dump=final,
            analysis_manifest=manifest,
            reasons=tuple(combined),
        )

    def _local_wiring(self, run_dir: Path) -> HeapAnalysisWiring:
        """Prepare images, deploy, and bind the owned local session and MAT."""
        from nanolab.plans.soak import compose_frozen_soak_workflow
        from nanolab.tasks.soak.preparation import prepare_soak
        from nanolab.tasks.soak.runtime import create_local_deployment
        from nanolab.tasks.soak.teardown import cleanup_timeout_s

        preparation = self.options.preparation
        # Build the helper first: prepare_soak needs its digest in the
        # diagnostic policy, and MAT needs the same one later. Freezing it here
        # keeps both ends of the run on one immutable image without committing
        # a digest that only exists in the builder's own registry.
        helper_image = self.options.helper_image or build_helper_image(
            HelperImageRequest(
                repo_root=self.repo_root,
                run_dir=run_dir,
                run_id=run_dir.name,
                registry=preparation.registry.split("/", 1)[0] + "/nanolab",
                builder=self.options.helper_builder,
                image_name="diagnostic-helper",
            )
        )
        protocol = deployment_protocol(self.protocol, helper_image)
        # This local wiring IS the diagnostic provider: it deploys real Docker
        # containers and drives them through the session built below, so it
        # must declare itself available before prepare_soak's support check,
        # the way nanolab.tasks.soak.runtime does for soak's own diagnostics.
        if preparation.diagnostic_adapter is None:
            preparation = replace(preparation, diagnostic_provider_available=True)
        prepared = self.options.prepared or prepare_soak(
            protocol,
            run_dir=run_dir,
            repo_root=self.repo_root,
            tool_root=self.tool_root,
            options=preparation,
        )
        deployment = create_local_deployment(
            prepared,
            run_dir,
            docker_socket=self.options.docker_socket,
            allow_diagnostic_target_stop_on_cancel=True,
        )
        session = LocalHeapAnalysisSession(
            self.protocol,
            prepared,
            deployment,
            helper_image=helper_image,
            docker_socket=self.options.docker_socket,
            generator_command=self.options.preparation.generator_command,
        )

        def compose(measurement: Task[Any]) -> Workflow:
            return compose_frozen_soak_workflow(
                deployment.request,
                self.bindings,
                project=deployment.project,
                measurement=measurement,
                api_endpoint=deployment.api_endpoint,
                ownership=deployment.ownership,
                cwd=self.repo_root,
                workflow_id="heap-analysis",
                release_timeout_s=cleanup_timeout_s(protocol.cancellation_timeout_s),
            )

        return HeapAnalysisWiring(
            writer=prepared.writer,
            session=session,
            compose=compose,
            analyze=MatAnalyzer().run,
            helper_image=helper_image,
        )
