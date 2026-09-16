"""Prove the heap-analysis lifecycle's ordering and its terminal verdicts.

The order below is a safety contract, not a style: the natural drain reading
must be taken before any diagnostic GC perturbs it, and MAT must not start
while the measured deployment still holds its resources. Both are asserted
through the real Sonata runner, so the compiler enforces them rather than the
order of calls inside a fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sonata_engine import Resource, Task, TaskInputs, Workflow

from nanolab.config.heap_analysis import HeapAnalysisConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.heap_analysis.runtime import (
    HeapAnalysisOptions,
    HeapAnalysisResult,
    HeapAnalysisWiring,
    LocalHeapAnalysisSession,
    RunControlPlaneHeapAnalysis,
    deployment_protocol,
    workload_outcome,
)
from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.preparation import PreparedSoak

SAFETY_ORDER = [
    "deploy",
    "warmup",
    "observe:before-baseline",
    "gc:baseline",
    "dump:baseline",
    "steady",
    "drain",
    "observe:natural-drain",
    "gc:final",
    "dump:final",
    "observe:after-final-gc",
    "release-deployment",
    "mat",
    "cleanup-helper",
]


DIGEST = "localhost:5000/nanolab/heap-analysis-helper@sha256:" + "b" * 64


def heap_analysis_payload() -> dict[str, Any]:
    """Build a minimal valid protocol; durations stay tiny for tests."""
    return {
        "target": "control-plane",
        "warmup_s": 2,
        "steady_s": 4,
        "drain_s": 3,
        "roles": {
            "control-plane": {
                "runtime": "jvm",
                "expected_cpu": 2.0,
                "memory_limit_bytes": 1073741824,
                "required_metrics": [
                    "process_rss_bytes",
                    "cgroup_memory_usage_bytes",
                ],
                "required_capabilities": ["rss", "cgroup"],
                "runtime_options": ["-Xmx512m"],
                "collection_sources": ["procfs", "cgroup"],
            },
            "word-stats-java": {
                "runtime": "jvm",
                "expected_cpu": 2.0,
                "memory_limit_bytes": 1073741824,
                "required_metrics": [
                    "process_rss_bytes",
                    "cgroup_memory_usage_bytes",
                ],
                "required_capabilities": ["rss", "cgroup"],
                "runtime_options": [],
                "collection_sources": ["procfs", "cgroup"],
            },
            "word-stats-javascript": {
                "runtime": "node",
                "expected_cpu": 1.0,
                "memory_limit_bytes": 536870912,
                "required_metrics": [
                    "process_rss_bytes",
                    "cgroup_memory_usage_bytes",
                ],
                "required_capabilities": ["rss", "cgroup"],
                "runtime_options": [],
                "collection_sources": ["procfs", "cgroup"],
            },
        },
        "images": {
            "control-plane": {"variant": "jvm", "platform": "linux/amd64"},
            "word-stats-java": {"variant": "jvm", "platform": "linux/amd64"},
            "word-stats-javascript": {
                "variant": "default",
                "platform": "linux/amd64",
            },
        },
        "workload": {
            "rates": {"word-stats-java": 100, "word-stats-javascript": 100},
            "preallocated_vus": 200,
            "max_vus": 200,
            "max_error_ratio": 0,
            "max_dropped_iterations": 0,
        },
        "max_dumps": 2,
        "max_dump_bytes": 2147483648,
        "artifact_limit_bytes": 4294967296,
        "diagnostic_timeout_s": 60,
        "mat_memory_mib": 2048,
        "mat_cpus": 2.0,
        "mat_timeout_s": 300,
    }


def heap_analysis_config(**overrides: Any) -> HeapAnalysisConfig:
    return HeapAnalysisConfig.model_validate(heap_analysis_payload() | overrides)


def heap_analysis_scenario(**overrides: Any) -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {
            "workflow": "heap-analysis",
            "backend": "container",
            "functions": ["word-stats-java", "word-stats-javascript"],
            "heapAnalysis": heap_analysis_payload() | overrides,
        }
    )


def workload_receipt(*, completed: bool = True, errors: int = 0) -> dict[str, Any]:
    """Build a receipt in the shape K6WorkloadDriver actually publishes."""

    def counter(value: int | None) -> dict[str, Any]:
        return {
            "value": value,
            "availability": "observed" if value is not None else "unavailable",
        }

    return {
        "schema": "nanolab-soak-v1",
        "kind": "workload",
        "completed": completed,
        "threshold_failed": False,
        "errors": [],
        "counters": {
            "offered": counter(1000),
            "success": counter(1000 - errors),
            "error": counter(errors),
            "dropped": counter(0),
        },
    }


class FakeSession:
    """Record every ordered lifecycle action and publish plausible evidence."""

    def __init__(
        self,
        events: list[str],
        root: Path,
        *,
        raise_on: str | None = None,
        steady_errors: int = 0,
    ) -> None:
        self.events = events
        self.root = root
        self.raise_on = raise_on
        self.steady_errors = steady_errors
        self.closed = 0

    def _fail(self, event: str) -> None:
        if self.raise_on == event:
            raise RuntimeError(f"fake infrastructure failure: {event}")

    def load(self, phase: str, duration_s: float) -> Path:
        self.events.append(phase)
        self._fail(phase)
        assert duration_s > 0
        receipt = self.root / f"workload-{phase}.json"
        errors = self.steady_errors if phase == "steady" else 0
        receipt.write_text(json.dumps(workload_receipt(errors=errors)))
        return receipt

    def settle(self, duration_s: float) -> None:
        self.events.append("drain")
        self._fail("drain")
        assert duration_s > 0

    def observe(self, checkpoint: str) -> Path:
        self.events.append(f"observe:{checkpoint}")
        self._fail(f"observe:{checkpoint}")
        path = self.root / f"runtime-{checkpoint}.json"
        path.write_text(json.dumps({"checkpoint": checkpoint}))
        return path

    def full_gc(self, checkpoint: str) -> Path:
        self.events.append(f"gc:{checkpoint}")
        self._fail(f"gc:{checkpoint}")
        path = self.root / f"gc-{checkpoint}.json"
        path.write_text(json.dumps({"status": "PASS"}))
        return path

    def heap_dump(self, checkpoint: str) -> Path:
        self.events.append(f"dump:{checkpoint}")
        self._fail(f"dump:{checkpoint}")
        path = self.root / f"{checkpoint}.hprof"
        path.write_bytes(b"JAVA PROFILE 1.0.2\x00" + checkpoint.encode())
        return path

    def stop_load(self, timeout_s: float) -> None:
        self.events.append("stop-load")
        assert timeout_s > 0

    def close(self) -> None:
        self.closed += 1
        self.events.append("cleanup-helper")


class InterruptingSession(FakeSession):
    """Interrupt the steady load the way an operator's Ctrl-C would."""

    def load(self, phase: str, duration_s: float) -> Path:
        if phase == "steady":
            self.events.append(phase)
            raise KeyboardInterrupt("operator stopped the run")
        return super().load(phase, duration_s)


class Harness:
    """Fake wiring: a real Sonata deployment resource plus a fake session."""

    def __init__(self, tmp_path: Path, session: FakeSession) -> None:
        self.events = session.events
        self.session = session
        self.tmp_path = tmp_path
        self.held = False
        self.mat_saw_deployment_held: bool | None = None
        self.mat_error: Exception | None = None
        self.requests: list[Any] = []
        self.writer_closed = False

    def _deployment(self) -> Resource[str]:
        def acquire(inputs: TaskInputs) -> str:
            self.events.append("deploy")
            self.held = True
            return "deployment"

        def release(inputs: TaskInputs, value: str) -> None:
            self.held = False
            self.events.append("release-deployment")

        return Resource(
            title="Own fake heap-analysis deployment",
            acquire=acquire,
            release=release,
            always_release=True,
        )

    def compose(self, measurement: Task[Any]) -> Workflow:
        workflow = Workflow(workflow_id="heap-analysis")
        workflow.add(measurement, requires=(self._deployment(),))
        return workflow

    def analyze(self, request: Any) -> Path:
        self.mat_saw_deployment_held = self.held
        self.requests.append(request)
        self.events.append("mat")
        if self.mat_error is not None:
            raise self.mat_error
        analysis = request.output_dir / "analysis"
        analysis.mkdir()
        manifest = analysis / "manifest.json"
        manifest.write_text(json.dumps({"status": "PASS", "reports": {}}))
        return manifest

    def wiring(self, run_dir: Path) -> HeapAnalysisWiring:
        writer = ArtifactWriter(run_dir / "evidence", 1024 * 1024)
        close = writer.close

        def record() -> None:
            self.writer_closed = True
            close()

        writer.close = record  # type: ignore[method-assign]
        return HeapAnalysisWiring(
            writer=writer,
            session=self.session,
            compose=self.compose,
            analyze=self.analyze,
            helper_image=DIGEST,
        )


def build(
    tmp_path: Path,
    session: FakeSession,
    **config_overrides: Any,
) -> tuple[RunControlPlaneHeapAnalysis, Harness, Path]:
    harness = Harness(tmp_path, session)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    task = RunControlPlaneHeapAnalysis(
        heap_analysis_scenario(**config_overrides),
        bindings=None,
        run_dir=run_dir,
        repo_root=tmp_path,
        tool_root=tmp_path,
        options=HeapAnalysisOptions(wiring=harness.wiring),
    )
    return task, harness, run_dir


def measure(task: RunControlPlaneHeapAnalysis) -> HeapAnalysisResult:
    """Run the task once and unwrap the outcome the workflow published."""
    result = task.run(TaskInputs.empty()).value
    assert result is not None
    return result


def evidence(tmp_path: Path) -> Path:
    directory = tmp_path / "session"
    directory.mkdir()
    return directory


def test_lifecycle_follows_the_ordered_safety_sequence(tmp_path: Path) -> None:
    events: list[str] = []
    task, harness, run_dir = build(tmp_path, FakeSession(events, evidence(tmp_path)))

    result = measure(task)

    assert events == SAFETY_ORDER
    assert isinstance(result, HeapAnalysisResult)
    assert result.status == "PASS"
    assert result.baseline_dump is not None
    assert result.final_dump is not None
    assert result.analysis_manifest is not None
    assert harness.mat_saw_deployment_held is False
    assert json.loads((run_dir / "terminal.json").read_text())["status"] == "PASS"


def test_natural_drain_reading_precedes_the_final_gc(tmp_path: Path) -> None:
    events: list[str] = []
    task, _harness, _run_dir = build(tmp_path, FakeSession(events, evidence(tmp_path)))

    task.run(TaskInputs.empty())

    assert events.index("observe:natural-drain") < events.index("gc:final")
    assert events.index("drain") < events.index("observe:natural-drain")


def test_mat_never_starts_while_the_deployment_resource_is_held(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    task, harness, _run_dir = build(tmp_path, FakeSession(events, evidence(tmp_path)))

    task.run(TaskInputs.empty())

    assert harness.mat_saw_deployment_held is False
    assert events.index("release-deployment") < events.index("mat")


def test_report_states_that_pass_means_complete_diagnostics(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    task, _harness, run_dir = build(tmp_path, FakeSession(events, evidence(tmp_path)))

    result = measure(task)
    report = json.loads(Path(result.report).read_text())

    # The design's stable artifact set puts report.json at the run root, beside
    # the journal and terminal.json, not inside the bounded evidence tree.
    assert result.report == run_dir / "report.json"
    assert not (run_dir / "evidence" / "report.json").exists()
    assert json.loads((run_dir / "terminal.json").read_text())["report_path"] == str(
        run_dir / "report.json"
    )
    assert report["status"] == "PASS"
    assert report["sensitive"] is True
    meaning = report["meaning"].lower()
    assert "complete" in meaning
    assert "no-leak" in meaning or "no leak" in meaning
    assert "p24" in meaning


def test_interrupted_load_stops_the_generator_and_releases_the_deployment(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    task, harness, run_dir = build(
        tmp_path, InterruptingSession(events, evidence(tmp_path))
    )

    with pytest.raises(KeyboardInterrupt):
        task.run(TaskInputs.empty())

    assert "stop-load" in events
    assert events.index("stop-load") < events.index("release-deployment")
    assert events[-1] == "cleanup-helper"
    assert "mat" not in events
    assert harness.held is False
    assert json.loads((run_dir / "terminal.json").read_text())["status"] == "ABORTED"


@pytest.mark.parametrize(
    "failure",
    ["dump:baseline", "dump:final", "gc:final", "observe:natural-drain"],
)
def test_diagnostic_infrastructure_failure_is_inconclusive(
    tmp_path: Path, failure: str
) -> None:
    events: list[str] = []
    task, harness, run_dir = build(
        tmp_path, FakeSession(events, evidence(tmp_path), raise_on=failure)
    )

    result = measure(task)

    assert result.status == "INCONCLUSIVE"
    assert result.analysis_manifest is None
    assert "mat" not in events
    assert events[-1] == "cleanup-helper"
    assert harness.held is False
    assert json.loads((run_dir / "terminal.json").read_text())["status"] == (
        "INCONCLUSIVE"
    )
    assert any(failure in reason for reason in result.reasons)


def test_mat_failure_is_inconclusive_and_still_cleans_up(tmp_path: Path) -> None:
    events: list[str] = []
    task, harness, _run_dir = build(tmp_path, FakeSession(events, evidence(tmp_path)))
    harness.mat_error = RuntimeError("MAT container did not complete")

    result = measure(task)

    assert result.status == "INCONCLUSIVE"
    assert result.analysis_manifest is None
    assert result.baseline_dump is not None
    assert events[-1] == "cleanup-helper"
    assert harness.session.closed == 1


def test_incomplete_mat_manifest_is_inconclusive(tmp_path: Path) -> None:
    events: list[str] = []
    task, harness, _run_dir = build(tmp_path, FakeSession(events, evidence(tmp_path)))
    original = harness.analyze

    def analyze(request: Any) -> Path:
        manifest = original(request)
        manifest.write_text(json.dumps({"status": "FAIL", "reports": {}}))
        return manifest

    harness.analyze = analyze  # pyright: ignore[reportAttributeAccessIssue]

    result = measure(task)

    assert result.status == "INCONCLUSIVE"
    assert result.analysis_manifest is not None


def test_workload_contract_violation_fails_after_capturing_evidence(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    task, _harness, run_dir = build(
        tmp_path, FakeSession(events, evidence(tmp_path), steady_errors=7)
    )

    result = measure(task)

    assert events == SAFETY_ORDER
    assert result.status == "FAIL"
    assert result.baseline_dump is not None
    assert result.final_dump is not None
    assert result.analysis_manifest is not None
    assert any("request failures" in reason for reason in result.reasons)
    assert json.loads((run_dir / "terminal.json").read_text())["status"] == "FAIL"


def test_mat_request_is_bounded_by_the_configured_budgets(tmp_path: Path) -> None:
    events: list[str] = []
    task, harness, run_dir = build(tmp_path, FakeSession(events, evidence(tmp_path)))

    task.run(TaskInputs.empty())

    request = harness.requests[0]
    assert request.helper_image == DIGEST
    assert request.mat_memory_mib == 2048
    assert request.mat_timeout_s == 300
    assert request.output_dir == run_dir
    # No dump-size budget: MAT analyzes whatever capture produced.
    assert not hasattr(request, "max_dump_bytes")
    assert request.baseline_hprof.is_file()
    assert request.final_hprof.is_file()


def test_lifecycle_refuses_to_restart(tmp_path: Path) -> None:
    events: list[str] = []
    task, _harness, _run_dir = build(tmp_path, FakeSession(events, evidence(tmp_path)))

    task.run(TaskInputs.empty())
    with pytest.raises(RuntimeError, match="cannot resume or restart"):
        task.run(TaskInputs.empty())


def test_deployment_protocol_carries_no_acceptance_policy() -> None:
    config = heap_analysis_config(
        roles=heap_analysis_payload()["roles"]
        | {
            name: heap_analysis_payload()["roles"]["control-plane"]
            | {"runtime": "node"}
            for name in ("word-stats-java", "word-stats-javascript")
        },
        images={
            name: {"variant": "jvm", "platform": "linux/amd64"}
            for name in (
                "control-plane",
                "word-stats-java",
                "word-stats-javascript",
            )
        },
    )

    protocol = deployment_protocol(config, DIGEST)

    assert protocol.purpose == "diagnostic"
    assert protocol.criteria == []
    assert protocol.retention_s == {}
    assert protocol.diagnostics.operations == {"control-plane": ["gc", "heap_dump"]}
    assert protocol.diagnostics.max_dumps == 2
    assert protocol.diagnostics.helper_images == {"control-plane": DIGEST}
    assert protocol.phases.steady_s == config.steady_s
    assert protocol.phases.drain_s == config.drain_s
    assert protocol.prerequisites.required_coverage == []


@pytest.mark.parametrize(
    ("mutate", "expected", "fragment"),
    [
        (lambda r: r.update(completed=False), "INCONCLUSIVE", "did not complete"),
        (
            lambda r: r["counters"]["error"].update(availability="unavailable"),
            "INCONCLUSIVE",
            "counters were not observed",
        ),
        (
            lambda r: r["counters"]["dropped"].update(value=3),
            "FAIL",
            "dropped 3 scheduled iterations",
        ),
        (
            lambda r: r.update(threshold_failed=True),
            "FAIL",
            "breached its declared thresholds",
        ),
        (
            lambda r: r["errors"].append("aggregate != per-function sum"),
            "FAIL",
            "aggregate != per-function sum",
        ),
        (lambda r: None, "PASS", None),
    ],
)
def test_workload_outcome_separates_violation_from_absent_evidence(
    tmp_path: Path, mutate: Any, expected: str, fragment: str | None
) -> None:
    receipt = workload_receipt()
    mutate(receipt)
    path = tmp_path / "workload-receipt.json"
    path.write_text(json.dumps(receipt))

    status, reasons = workload_outcome(path)

    assert status == expected
    if fragment is None:
        assert reasons == ()
    else:
        assert any(fragment in reason for reason in reasons)


def test_unreadable_workload_receipt_is_inconclusive(tmp_path: Path) -> None:
    path = tmp_path / "workload-receipt.json"
    path.write_text("not json")

    status, reasons = workload_outcome(path)

    assert status == "INCONCLUSIVE"
    assert any("unreadable" in reason for reason in reasons)


def test_a_run_that_cannot_be_wired_records_an_inconclusive_receipt(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def wiring(_run_dir: Path) -> Any:
        raise RuntimeError("docker socket is unavailable")

    task = RunControlPlaneHeapAnalysis(
        heap_analysis_scenario(),
        bindings=None,
        run_dir=run_dir,
        repo_root=tmp_path,
        tool_root=tmp_path,
        options=HeapAnalysisOptions(wiring=wiring),
    )

    with pytest.raises(RuntimeError, match="docker socket is unavailable"):
        task.run(TaskInputs.empty())

    terminal = json.loads((run_dir / "terminal.json").read_text())
    assert terminal["status"] == "INCONCLUSIVE"


# --- LocalHeapAnalysisSession: the parts that need no Docker ----------------
#
# These exercise the real class, not FakeSession, which shares none of its
# implementation. Only _bind/_capture and the container calls they make stay
# uncovered; the guards below are ordinary Python and are testable today.


def three_role_scenario() -> ScenarioConfig:
    """Build a protocol whose roles cover the control plane and both functions."""
    functions = ("word-stats-java", "word-stats-javascript")
    control_plane = heap_analysis_payload()["roles"]["control-plane"]
    return heap_analysis_scenario(
        roles={"control-plane": control_plane}
        | {name: control_plane | {"runtime": "node"} for name in functions},
        images={
            name: {"variant": "jvm", "platform": "linux/amd64"}
            for name in ("control-plane", *functions)
        },
    )


class FakeDeployment:
    """Enough of RuntimeDeployment to construct and drive the real session."""

    api_endpoint = "http://127.0.0.1:18080"

    def __init__(self, diagnostic_inputs: dict[str, Any] | None) -> None:
        self.diagnostic_inputs = diagnostic_inputs
        self.observed = 0
        self.project = SimpleNamespace(name="test-run")

    def discover(self) -> tuple[Any, ...]:
        return ()

    def observations(self, targets: tuple[Any, ...]) -> dict[str, Any]:
        self.observed += 1
        return {"roles": {}}


class FakeHelper:
    """A provisioned helper double: the only thing close() must act on."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeDriver:
    """A generator double recording every stop request it receives."""

    def __init__(self) -> None:
        self.stops: list[float] = []

    def stop(self, timeout_s: float) -> None:
        self.stops.append(timeout_s)


def local_session(
    tmp_path: Path, *, diagnostic_inputs: dict[str, Any] | None = None
) -> tuple[LocalHeapAnalysisSession, FakeDeployment]:
    """Build the real session over a fake deployment and real prepared inputs."""
    from nanolab.tasks.soak.images import BuildReceipt
    from nanolab.tasks.soak.sources import SourceSnapshot

    scenario = three_role_scenario()
    assert scenario.heap_analysis is not None
    protocol = scenario.heap_analysis
    digest = "localhost:5000/nanofaas/x@sha256:" + "b" * 64
    receipts = tuple(
        BuildReceipt(
            role=role,
            image_digest=digest,
            source_fingerprint="source",
            recipe_fingerprint="recipe",
            build_fingerprint="build",
            platform="linux/amd64",
            toolchains=(),
            base_images=(),
            logs=(),
        )
        for role in protocol.roles
    )
    snapshot = SourceSnapshot(
        root=tmp_path,
        fingerprint="source",
        revision=None,
        dirty=False,
        entries=(),
        manifest_path=tmp_path / "manifest.jsonl",
        manifest_sha256="0" * 64,
    )
    prepared = PreparedSoak(
        run_id="heap-analysis-test",
        config=deployment_protocol(protocol, DIGEST),
        evidence_dir=tmp_path / "evidence",
        writer=ArtifactWriter(tmp_path / "evidence", 1024 * 1024),
        snapshot=snapshot,
        recipes=(),
        receipts=receipts,
        payloads={
            name: [{"input": {"text": "a"}, "expected": {"words": 1}}]
            for name in protocol.workload.rates
        },
        builds_finished_s=0.0,
        frozen_at_s=0.0,
    )
    deployment = FakeDeployment(diagnostic_inputs)
    return (
        LocalHeapAnalysisSession(protocol, prepared, deployment, helper_image=DIGEST),
        deployment,
    )


def control_plane_decision() -> dict[str, Any]:
    """Build a plausible `_diagnostic_resource_inputs` decision for control-plane."""
    return {
        "runtime": "jvm",
        "operations": ["gc", "heap_dump"],
        "helper_image": DIGEST,
        "quota_bytes": 4096,
        "helper_headroom_bytes": 64 * 1024 * 1024,
        "helper_memory_bytes": 4096 + 64 * 1024 * 1024,
        "uid": 1000,
        "gid": 1000,
        "volume_key": "diagnostic-tmp-0",
        "target_tmp_volume": "test-run_diagnostic-tmp-0",
    }


def test_bind_provisions_a_helper_output_root_that_contains_every_capture_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real run demonstrated a crash caused by a helper mount mismatch.

    `_bind` provisioned the helper with
    `<evidence>/diagnostics` as its allowed output root, but `_capture`
    writes each checkpoint's GC/dump receipt to
    `<evidence>/<checkpoint>-<operation>` -- a sibling of that root, not a
    child of it. The executor's own containment check
    (`diagnostic_exec.py`'s `execute`) then rejects every single capture
    with "diagnostic artifacts require a fresh owned output directory",
    so no real GC or heap dump ever completes.
    """
    import nanolab.tasks.heap_analysis.runtime as heap_runtime

    session, deployment = local_session(
        tmp_path,
        diagnostic_inputs={
            "roles": {"control-plane": control_plane_decision()},
            "available_artifact_bytes": 1024 * 1024 * 1024,
        },
    )
    deployment.discover = lambda: (  # type: ignore[method-assign]
        Target(
            role="control-plane",
            container_id="c" * 64,
            process_id=1,
            process_started_at="2026-09-15T00:00:00Z",
            image_digest=DIGEST,
            runtime="jvm",
        ),
    )
    captured: dict[str, Any] = {}

    class FakeDiagnosticHelper:
        def close(self) -> None:
            pass

        def adapter(self, **_kwargs: Any) -> None:
            return None

    class FakeProvisioner:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def prepare(self, spec: Any, *, timeout_s: float) -> FakeDiagnosticHelper:
            captured["output_root"] = spec.output_root
            return FakeDiagnosticHelper()

    monkeypatch.setattr(
        heap_runtime, "LocalDockerDiagnosticProvisioner", FakeProvisioner
    )

    session._bind()

    output_root = captured["output_root"].resolve()
    for checkpoint in ("baseline", "final"):
        for operation in ("gc", "heap_dump"):
            output = session._root / f"{checkpoint}-{operation}"
            assert output.resolve().is_relative_to(output_root), (
                f"{checkpoint}-{operation} capture dir is not inside the "
                "helper's provisioned output root"
            )


def test_bind_sizes_the_dump_budget_for_every_planned_dump_not_just_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real run demonstrated a crash caused by a dump budget mis-sizing.

    heap-analysis takes `max_dumps` heap
    dumps from a single role (baseline, then final), but `_bind` sized the
    shared `DiagnosticBudget`'s total dump-byte ceiling to
    `config.max_dump_bytes` -- the SAME value used as each dump's own
    per-capture quota (`decision["quota_bytes"]`) whenever, as here, that
    quota is derived at the full `max_dump_bytes` ceiling. The first
    reservation (baseline) then exhausts the entire shared ceiling, and
    the second (final) always fails with "diagnostic dump budget
    exhausted" -- regardless of `max_dumps`.
    """
    import nanolab.tasks.heap_analysis.runtime as heap_runtime

    quota_bytes = heap_analysis_payload()["max_dump_bytes"]
    decision = control_plane_decision() | {
        "quota_bytes": quota_bytes,
        "helper_memory_bytes": quota_bytes + 64 * 1024 * 1024,
    }
    session, deployment = local_session(
        tmp_path,
        diagnostic_inputs={
            "roles": {"control-plane": decision},
            "available_artifact_bytes": 8 * quota_bytes,
        },
    )
    deployment.discover = lambda: (  # type: ignore[method-assign]
        Target(
            role="control-plane",
            container_id="c" * 64,
            process_id=1,
            process_started_at="2026-09-15T00:00:00Z",
            image_digest=DIGEST,
            runtime="jvm",
        ),
    )
    captured: dict[str, Any] = {}

    class FakeDiagnosticHelper:
        def close(self) -> None:
            pass

        def adapter(self, *, budget: Any, **_kwargs: Any) -> None:
            captured["budget"] = budget
            return

    class FakeProvisioner:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def prepare(self, spec: Any, *, timeout_s: float) -> FakeDiagnosticHelper:
            return FakeDiagnosticHelper()

    monkeypatch.setattr(
        heap_runtime, "LocalDockerDiagnosticProvisioner", FakeProvisioner
    )

    session._bind()

    budget = captured["budget"]
    assert session._config.max_dumps == 2
    budget.reserve(quota_bytes, dump=True)  # baseline
    budget.reserve(quota_bytes, dump=True)  # final: must not exhaust early


def test_local_wiring_declares_the_diagnostic_provider_before_preparing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real (uninjected) wiring path must satisfy check_preparation_support.

    `_local_wiring` is what every real run takes -- nothing else declares a
    diagnostic adapter or provider for it. Because it deploys real Docker
    containers and drives them through the diagnostic session it builds
    right below, it must declare the diagnostic provider available before
    calling `prepare_soak`, exactly as `nanolab.tasks.soak.runtime` does for
    the analogous soak diagnostics path. Without that declaration,
    `check_preparation_support` always raises "diagnostic adapter
    unavailable" for every real heap-analysis run.
    """
    import nanolab.tasks.soak.preparation as preparation_module
    import nanolab.tasks.soak.runtime as soak_runtime_module
    from nanolab.tasks.soak.images import BuildReceipt
    from nanolab.tasks.soak.sources import SourceSnapshot

    scenario = three_role_scenario()
    assert scenario.heap_analysis is not None
    protocol = scenario.heap_analysis
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    captured: dict[str, Any] = {}

    def fake_prepare_soak(
        config: Any,
        *,
        run_dir: Path,
        repo_root: Path,
        tool_root: Path,
        options: Any = None,
        **_: Any,
    ) -> PreparedSoak:
        # This is the exact condition check_preparation_support enforces for
        # the diagnostic-adapter clause (preparation.py:69-74); asserted here
        # directly so the test never has to reach real Docker/registry
        # infrastructure through the rest of that function's checks.
        assert options is not None
        assert options.diagnostic_adapter is not None or (
            options.diagnostic_provider_available is True
        ), "diagnostic adapter unavailable; provision it before building"
        captured["options"] = options
        digest = "localhost:5000/nanofaas/x@sha256:" + "b" * 64
        receipts = tuple(
            BuildReceipt(
                role=role,
                image_digest=digest,
                source_fingerprint="s",
                recipe_fingerprint="r",
                build_fingerprint="b",
                platform="linux/amd64",
                toolchains=(),
                base_images=(),
                logs=(),
            )
            for role in protocol.roles
        )
        snapshot = SourceSnapshot(
            root=tmp_path,
            fingerprint="s",
            revision=None,
            dirty=False,
            entries=(),
            manifest_path=tmp_path / "manifest.jsonl",
            manifest_sha256="0" * 64,
        )
        return PreparedSoak(
            run_id="test",
            config=config,
            evidence_dir=tmp_path / "evidence",
            writer=ArtifactWriter(tmp_path / "evidence", 1024 * 1024),
            snapshot=snapshot,
            recipes=(),
            receipts=receipts,
            payloads={name: [] for name in protocol.workload.rates},
            builds_finished_s=0.0,
            frozen_at_s=0.0,
        )

    monkeypatch.setattr(preparation_module, "prepare_soak", fake_prepare_soak)
    monkeypatch.setattr(
        soak_runtime_module,
        "create_local_deployment",
        lambda *args, **kwargs: FakeDeployment(None),
    )

    task = RunControlPlaneHeapAnalysis(
        scenario,
        bindings=None,
        run_dir=run_dir,
        repo_root=tmp_path,
        # An already-published helper digest, so this test exercises the
        # provider declaration rather than a real buildx build.
        options=HeapAnalysisOptions(helper_image=DIGEST),
        tool_root=tmp_path,
    )

    task._local_wiring(run_dir)

    assert captured  # prepare_soak actually ran and its check passed


def test_session_refuses_a_deployment_without_diagnostic_inputs(
    tmp_path: Path,
) -> None:
    session, deployment = local_session(tmp_path)

    with pytest.raises(RuntimeError, match="no control-plane diagnostic inputs"):
        session.observe("before-baseline")

    # The observation itself is published before the helper is ever needed, so
    # the failure is about provisioning, not about losing the evidence.
    assert deployment.observed == 1
    assert (tmp_path / "evidence" / "runtime-before-baseline.json").is_file()


def test_session_refuses_diagnostic_inputs_naming_another_role(
    tmp_path: Path,
) -> None:
    session, _deployment = local_session(
        tmp_path, diagnostic_inputs={"roles": {"word-stats-java": {}}}
    )

    with pytest.raises(RuntimeError, match="no control-plane diagnostic inputs"):
        session.observe("before-baseline")


def test_session_close_removes_the_helper_exactly_once(tmp_path: Path) -> None:
    session, _deployment = local_session(tmp_path)
    helper = FakeHelper()
    session._helper = helper

    session.close()
    session.close()

    assert helper.closed == 1


def test_session_close_without_a_helper_is_a_silent_no_op(tmp_path: Path) -> None:
    session, _deployment = local_session(tmp_path)

    session.close()
    session.close()


def test_session_stop_load_is_repeatable_and_cancels_the_run(tmp_path: Path) -> None:
    session, _deployment = local_session(tmp_path)
    driver = FakeDriver()
    session._driver = driver

    session.stop_load(5.0)
    session.stop_load(5.0)

    assert driver.stops == [5.0, 5.0]
    assert session._cancelled.is_set()
    # A cancelled run refuses to sit out the rest of the natural drain.
    with pytest.raises(KeyboardInterrupt, match="cancelled during natural drain"):
        session.settle(30.0)


def test_session_stop_load_before_any_generator_started_is_safe(
    tmp_path: Path,
) -> None:
    session, _deployment = local_session(tmp_path)

    session.stop_load(5.0)

    assert session._cancelled.is_set()


def test_task_refuses_a_scenario_that_is_not_heap_analysis(tmp_path: Path) -> None:
    scenario = ScenarioConfig.model_validate(
        {"workflow": "loadtest", "backend": "k8s", "functions": ["word-stats-java"]}
    )

    with pytest.raises(ValueError, match="requires a heap-analysis scenario"):
        RunControlPlaneHeapAnalysis(
            scenario,
            bindings=None,
            run_dir=tmp_path,
            repo_root=tmp_path,
            tool_root=tmp_path,
        )


def test_unconfirmed_helper_cleanup_downgrades_an_otherwise_complete_run(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class UncleanSession(FakeSession):
        def close(self) -> None:
            super().close()
            raise RuntimeError("helper container is still running")

    task, _harness, run_dir = build(
        tmp_path, UncleanSession(events, evidence(tmp_path))
    )

    result = measure(task)

    # Every measured step still ran, but a run whose helper may survive is not
    # a complete diagnostic.
    assert events == SAFETY_ORDER
    assert result.status == "INCONCLUSIVE"
    assert any("helper cleanup unconfirmed" in reason for reason in result.reasons)
    assert json.loads((run_dir / "terminal.json").read_text())["status"] == (
        "INCONCLUSIVE"
    )


def test_failed_report_publication_still_closes_evidence_and_writes_terminal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Without the finally, the CLI is left with no terminal.json to read."""
    from nanolab.tasks.heap_analysis import runtime as runtime_module

    task, harness, run_dir = build(tmp_path, FakeSession([], evidence(tmp_path)))
    real = runtime_module._publish_run_document

    def publish(directory: Path, name: str, payload: Any) -> Path:
        if name == "report.json":
            raise OSError("no space left on device")
        return real(directory, name, payload)

    monkeypatch.setattr(runtime_module, "_publish_run_document", publish)
    with pytest.raises(OSError, match="no space left"):
        task.run(TaskInputs.empty())
    terminal = json.loads((run_dir / "terminal.json").read_text())
    assert terminal["status"] == "INCONCLUSIVE"
    assert harness.writer_closed
