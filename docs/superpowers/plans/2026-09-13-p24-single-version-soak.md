# P24 Single-Version Soak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a reusable NanoLab soak workflow that measures one NanoFaaS version, enforces the P24 evidence contract, and preserves useful evidence on every exit path.

**Architecture:** Compose a dedicated Sonata workflow from existing NanoLab platform and workload primitives. Keep configuration, process probes, runtime diagnostics, bounded artifact writing and offline evaluation separate. The observer owns the entire measurement interval, not just the k6 invocation.

**Tech Stack:** Existing Python >=3.12 workspace, Pydantic, Sonata Engine/tasks pinned by NanoLab, Docker/Compose, k6, pytest, existing HTTP/Prometheus support; runtime-specific diagnostic tools only after capability checks.

**Spec:** `docs/superpowers/specs/2026-09-13-p24-single-version-soak-design.md` (approved by the user on 2026-09-13).

## Global Constraints

- "Il workflow non confronta revisioni, non gestisce bracci baseline/candidate e non dichiara chiusa l'intera campagna."
- "Warm-up, diagnosi e drain non consumano i 90 minuti."
- "Le soglie richieste devono essere presenti prima del run."
- "Non sommare heap, RSS e cgroup."
- "Una metrica obbligatoria assente non equivale a zero."
- "Il profilo native non simula metriche JVM."
- "Il successo HTTP e la stabilita' di RSS non bastano."
- "Un RSS residuo sospetto non attribuito impedisce PASS anche quando le metriche applicative rientrano."
- "Un run interrotto non puo' essere ripreso e presentato come un soak continuo: la nuova esecuzione ha un nuovo identificatore."
- First executable backend is container/local. Do not advertise Kubernetes support.
- Do not modify NanoFaaS implementation, Sonata dependency pins, unrelated loadtest behavior or user experiment evidence as part of this plan.
- No automatic long experiment, image build, commit or destructive cleanup as part of planning. During implementation, ask for authorization for tests/real runs when the active session requires it. Commits require explicit user authorization.
- Before editing existing symbols, follow repository impact-analysis instructions. NanoLab was absent from the available GitNexus registry during planning; this is unresolved graph coverage, not LOW risk. Do not borrow the nanofaas graph for NanoLab.

## Execution Context and Known Integration Points

All paths below are relative to `/home/michele/Documenti/nanolab`. NanoFaaS is `/home/michele/Documenti/nanofaas`; export `NANOFAAS_ROOT` when executing repository-dependent commands. This checkout can require separate filesystem write approval.

Known source behavior:

- `config/scenario.py`: `WorkflowName` has no soak; `ScenarioConfig` has legacy `soakMinutes`/`drainMinutes`, and mixed/comparison validation requires two functions.
- `cli/product.py`: `_workflow` falls through to comparison/loadtest after explicit branches; run metadata and selection/resume policy must not mislabel a partial soak as successful.
- `tasks/loadtest/__init__.py`: `RunK6Task` starts/stops its sampler around k6; it cannot own a soak observer that must survive into drain. `build_loadtest_workflow` retains platform/function resources through the load composite using `add_platform`.
- `tasks/loadtest/soak.py`: existing observer saves at the end, sums labels and samples only control-plane metrics; do not use it unchanged as the P24 collector.
- `tasks/loadtest/resources.py`: existing Docker metrics include a derived working set; do not relabel that field RSS or raw cgroup usage.
- `tasks/compose.py`: existing isolated Compose resource is a reuse candidate. `tasks/platform.py` owns platform/function resources. `tasks/k6.py` adapts the shared k6 task.
- `tui/app.py` also dispatches loadtest; new workflow routing must not silently fall into that path.
- Existing generator: `packages/nanolab/assets/k6/mixed-workload.js`. Reuse payload/correctness semantics, not comparison-specific build orchestration.

Read each implementation target and applicable local instructions once at the start of its task, including the pinned Sonata APIs actually used. The source exploration in planning was not a complete dependency audit. Do not invent Resource lifecycle methods or assume the shared subprocess executor propagates cancellation correctly: task 7 tests that contract.

## File Map

Create under `packages/nanolab/src/nanolab/`:

| Path | Responsibility |
| --- | --- |
| `config/soak.py` | Validated soak policy models |
| `tasks/soak/__init__.py` | Small public exports |
| `tasks/soak/models.py` | Samples, targets, verdicts, evidence identities |
| `tasks/soak/ports.py` | Clock, process probe, diagnostic and workload contracts |
| `tasks/soak/artifacts.py` | Incremental writes, manifest and artifact identities |
| `tasks/soak/preflight.py` | Effective limits, provenance and capability checks |
| `tasks/soak/probes.py` | Docker, procfs and label-preserving exposition adapters |
| `tasks/soak/observer.py` | Bounded continuous sampling |
| `tasks/soak/diagnostics.py` | Runtime-specific checkpoint adapters |
| `tasks/soak/workload.py` | Constant load and bounded generator lifetime |
| `tasks/soak/prerequisites.py` | Short-profile evidence receipts |
| `tasks/soak/evaluate.py` | Pure offline policy evaluation |
| `tasks/soak/report.py` | JSON and readable report |
| `tasks/soak/workflow.py` | Sonata measurement lifetime and phase tasks |
| `plans/soak.py` | Scenario-to-Sonata wiring |
| `cli/soak.py` | Offline evaluation command and soak CLI guards |

Modify only the required integration points: `config/scenario.py`, `cli/product.py`, `tui/app.py`, the two existing soak YAML presets and root `README.md`. Reuse `tasks/platform.py`, `tasks/compose.py` and `tasks/k6.py` without edits if their public contracts suffice; if an adapter change is necessary, perform impact analysis and add the regression to that task before editing. No broad loadtest refactor.

New tests live in `packages/nanolab/tests/soak/`, plus `tests/config/test_soak.py`, `tests/plans/test_soak.py` and `tests/test_soak_cli.py`. Create `docs/soak.md` and a labeled smoke scenario `packages/nanolab/scenarios-v2/memory-soak-smoke-container.yaml`.

## Shared Contracts

Use frozen dataclasses for runtime values and strict Pydantic models for external JSON/YAML. Persist schema identifier `nanolab-soak-v1`; reject unsupported schema versions.

`models.py` defines:

```python
from dataclasses import dataclass
from typing import Literal

Status = Literal["PASS", "FAIL", "INCONCLUSIVE", "ABORTED"]
Availability = Literal["observed", "unavailable", "not_applicable"]
Phase = Literal["preflight", "warmup", "baseline", "steady", "drain", "diagnostic"]

@dataclass(frozen=True)
class Target:
    role: str
    container_id: str
    process_id: int
    process_started_at: str
    image_digest: str
    runtime: str

@dataclass(frozen=True)
class Sample:
    target: Target
    phase: Phase
    scheduled_s: float
    started_s: float
    ended_s: float
    metric: str
    labels: tuple[tuple[str, str], ...]
    unit: str
    value: float | None
    availability: Availability
    source: str
    reason: str | None

@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    status: Status
    reason: str
    evidence: tuple[str, ...]
```

`config/soak.py` defines `SoakConfig`, `PhaseConfig`, `RolePolicy`, `Criterion`, `DiagnosticPolicy` and `PrerequisitePolicy`. Reject extra fields. Public configuration is snake_case inside the new `soak` block; existing top-level aliases stay unchanged.

Required `SoakConfig` fields: `purpose` (`p24` or `smoke`), `phases`, `retention_s` (owner-to-positive-seconds), `roles` (role-to-RolePolicy), `criteria`, `diagnostics`, `prerequisites`, `sample_interval_s`, `scrape_timeout_s`, `max_observation_gap_s`, `artifact_limit_bytes`, `cancellation_timeout_s`, `workload` (explicit per-function requests/second and VU capacity). `PhaseConfig` contains `warmup_s`, `baseline_drain_s`, `steady_s`, `drain_s`, `cleanup_margin_s` and `baseline_window_s`.

`RolePolicy` contains required metrics/capabilities, expected CPU and memory limit, runtime options and collection sources. `Criterion` contains unique `id`, role, metric, exact label selector, operation, phase, window/deadline, explicit threshold/tolerance and rationale. Operations: `maximum`, `return_to_reference`, `growth_review`, `expected_zero`. For `return_to_reference`, require both absolute-byte and relative tolerances and use the stricter bound; reference is the median of the declared natural baseline window, evaluated against the maximum in the declared natural final window. Do not evaluate RSS recovery using post-dump samples.

`DiagnosticPolicy` declares executable/helper image identities, supported operations, per-operation timeout, maximum dump count/bytes and post-GC completion evidence. `PrerequisitePolicy` maps required coverage IDs to receipt paths and relevant configuration keys; no implicit exemption for missing receipts. An empty requirement list is valid only for `smoke`.

`ports.py` defines Protocols with these signatures:

- `Clock.monotonic() -> float`; `Clock.wait_until(deadline_s: float, cancelled: Event) -> bool` returns false on cancellation.
- `ProcessProbe.targets() -> tuple[Target, ...]`; `ProcessProbe.sample(target: Target, phase: Phase, scheduled_s: float) -> tuple[Sample, ...]`.
- `DiagnosticAdapter.capabilities(target: Target) -> frozenset[str]`; `DiagnosticAdapter.capture(target: Target, checkpoint: str, output_dir: Path, timeout_s: float) -> Path` returns a receipt with actual completion status, not an implied success.
- `WorkloadDriver.run(output_dir: Path, duration_s: float, cancelled: Event) -> Path` returns the workload receipt; `WorkloadDriver.stop(timeout_s: float) -> None` is idempotent and joins/reaps owned children.

## Task 1: Strict Scenario Contract and Temporal Validation

**Files:** Create `config/soak.py`, `tasks/soak/models.py`, `tasks/soak/ports.py`, `tasks/soak/__init__.py`, `tests/config/test_soak.py`; modify `config/scenario.py`.

**Interfaces:** Produce the shared contracts above and `validate_schedule(steady_s: int, drain_s: int, retention_s: tuple[int, ...], cleanup_margin_s: int) -> None` in `config/soak.py`.

- [ ] Write the RED temporal regression:

```python
import pytest
from nanolab.config.soak import validate_schedule

def test_p24_drain_must_include_cleanup_margin():
    with pytest.raises(ValueError, match="drain"):
        validate_schedule(5400, 1800, (30, 300, 1800), 300)

def test_historical_schedule_covers_three_cycles():
    validate_schedule(5400, 2100, (30, 300, 1800), 300)
```

- [ ] Run `uv run --locked --package nanolab pytest -c packages/nanolab/pyproject.toml --no-cov packages/nanolab/tests/config/test_soak.py -q`; expect missing implementation first.
- [ ] Implement the strict models and schedule rule:

```python
longest = max(retention_s)
if steady_s < 3 * longest:
    raise ValueError("steady must cover three retention cycles")
if drain_s < longest + cleanup_margin_s:
    raise ValueError("drain must include retention and cleanup margin")
```

- [ ] Add `soak` to `WorkflowName` and optional `soak: SoakConfig` to `ScenarioConfig`. Require it for soak, forbid it elsewhere. Reject non-container backend, governors, autoscaling, mixed/comparison-only flags and missing per-role limits. The new workflow must not inherit the legacy exactly-two-functions restriction. Reject legacy soakMinutes/drainMinutes on the new workflow rather than silently prioritizing one configuration.
- [ ] Add parameterized tests for missing metrics/criteria, duplicate criterion IDs, nonfinite numbers, zero/negative durations, invalid role selectors, insufficient baseline drain, sample interval larger than permitted gap and smoke/P24 distinction. Smoke can shorten time but cannot produce P24 coverage.
- [ ] Run new tests plus `tests/config/test_scenario.py`. Gate: old scenarios unchanged, invalid soak rejected before side effects.

## Task 2: Durable Artifacts and Evidence Identities

**Files:** Create `tasks/soak/artifacts.py`, `tests/soak/test_artifacts.py`.

**Interfaces:** `ArtifactWriter(root: Path, limit_bytes: int)`; methods `append(stream: str, record: dict[str, object]) -> None`, `write_json(name: str, value: dict[str, object]) -> Path`, `close() -> None`. Functions `fingerprint(value: dict[str, object]) -> str` and `read_records(path: Path) -> Iterator[dict[str, object]]`.

- [ ] Write RED durability test:

```python
import json
from nanolab.tasks.soak.artifacts import ArtifactWriter

def test_events_exist_before_writer_close(tmp_path):
    writer = ArtifactWriter(tmp_path, limit_bytes=4096)
    writer.append("events", {"phase": "drain"})
    assert json.loads((tmp_path / "events.jsonl").read_text()) == {"phase": "drain"}
    writer.close()
```

- [ ] Run focused test, then implement one flushed JSONL record per append; use a lock for concurrent writers. Atomic replace for complete JSON documents, never for an entire growing stream. Hash canonical sorted JSON for evidence identity:

```python
body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
return hashlib.sha256(body.encode("utf-8")).hexdigest()
```

- [ ] Reject traversal/absolute stream names and existing run directories with evidence. Use exclusive creation for run ownership. Store large artifact sizes/checksums incrementally; do not read dumps wholesale into memory.
- [ ] Add tests for a torn final JSONL record (report gap, retain valid prefix), disk-full write failure, quota exceeded, immutable prior evaluations and concurrent append integrity. Reserve room for terminal status; if even that write fails, surface the error and preserve existing files.
- [ ] Run `... pytest ... --no-cov packages/nanolab/tests/soak/test_artifacts.py -q` using the full command prefix from task 1. Gate: recoverable partial evidence, bounded writer memory.

## Task 3: Effective Environment and Image Preflight

**Files:** Create `tasks/soak/preflight.py`, `tests/soak/test_preflight.py`.

**Interfaces:** `check_limits(expected: dict[str, float], actual: dict[str, float | None]) -> tuple[CriterionResult, ...]`; `preflight(config: SoakConfig, targets: tuple[Target, ...], observations: dict[str, object], writer: ArtifactWriter) -> tuple[CriterionResult, ...]`.

- [ ] Write RED for the original invalid-limit failure:

```python
from nanolab.tasks.soak.preflight import check_limits

def test_unlimited_container_does_not_satisfy_one_gib_limit():
    result = check_limits(
        {"memory_bytes": 1073741824.0, "cpu": 2.0},
        {"memory_bytes": None, "cpu": 2.0},
    )
    assert any(item.status == "INCONCLUSIVE" for item in result)
```

- [ ] Implement expected-versus-effective CPU quota/period, cpuset restrictions and memory limits. Normalize unlimited sentinel values to unavailable/unlimited, never expected values. Record the relevant host/cgroup source; Docker's display limit alone is insufficient proof.
- [ ] Resolve image tags to immutable digests before launch; confirm running IDs/digests, functions, selected modules and runtime/GC/heap options. Save source provenance plus dirty-source fingerprints using existing provenance facilities; never require a clean tree merely to run a valid locally built image.
- [ ] Capability preflight covers required metric sources, diagnostic helper availability/permissions, free space and generator executable/capacity. Validate effective retention through an authoritative observable configuration source. If a value cannot be verified, record it as missing and block P24 instead of trusting an intended environment variable blindly.
- [ ] Add mismatched SDK image, changed module configuration, missing retained-state metric, incorrect GC, unknown process, changed payload hash and expired/mismatched prerequisite tests.
- [ ] Run focused preflight tests. Gate: no long workload starts after preflight rejection; all observed/declared differences remain in evidence.

## Task 4: Process Probes and Bounded Continuous Observer

**Files:** Create `tasks/soak/probes.py`, `tasks/soak/observer.py`, `tests/soak/test_probes.py`, `tests/soak/test_observer.py`.

**Interfaces:** `parse_exposition(text: str) -> tuple[tuple[str, tuple[tuple[str, str], ...], float], ...]`; `sample_deadlines(start_s: float, end_s: float, interval_s: float) -> Iterator[float]`; `Observer(probe: ProcessProbe, clock: Clock, writer: ArtifactWriter, interval_s: float)` with `start(phase: Phase)`, `set_phase(phase: Phase)` and `stop(timeout_s: float)`.

- [ ] Write RED label-preservation and absolute-clock tests:

```python
from nanolab.tasks.soak.probes import parse_exposition
from nanolab.tasks.soak.observer import sample_deadlines

def test_heap_and_nonheap_are_not_collapsed():
    rows = parse_exposition('jvm_memory_used_bytes{area="heap"} 10\n'
                            'jvm_memory_used_bytes{area="nonheap"} 20\n')
    assert len(rows) == 2
    assert rows[0][1] != rows[1][1]

def test_schedule_does_not_accumulate_scrape_duration():
    assert list(sample_deadlines(100, 130, 10)) == [100, 110, 120, 130]
```

- [ ] Implement label-preserving parsing with support for escaped label values and optional timestamps. Prefer an already-installed public parser only after checking the locked dependency; otherwise add a scoped parser with malformed/nonfinite input tests. Missing and malformed required values produce explicit gaps.
- [ ] Implement Docker statistics plus procfs RSS/PSS sources through role-bound adapters; save raw cgroup fields and name working-set estimates honestly. Track process/container identity on every sample. Scrape each SDK/proxy, not only the control plane. Registry meter population requires its own source; exposition cardinality is not a replacement.
- [ ] Implement a single bounded observer lifetime across warm-up, baseline, steady and drain. Write each sample immediately. Skip/report missed deadlines rather than launching unbounded catch-up work. Use cancellation-aware waits and per-probe timeouts. Bound retained cardinality state; stream observations to disk and compute exact historical unions offline using disk-backed storage when needed.
- [ ] Add tests for unavailable process/PSS, restart, endpoint timeout, full disk, slow scrape, 100000 synthetic samples with fixed buffer limits, changing labels and observer cancellation during drain.
- [ ] Run both focused test files. Gate: last drain sample persists before observer stop and platform teardown.

## Task 5: Runtime Diagnostics and Attribution Receipts

**Files:** Create `tasks/soak/diagnostics.py`, `tests/soak/test_diagnostics.py`.

**Interfaces:** `gc_completed(before_count: int | None, after_count: int | None, requested_ok: bool) -> bool`; implementations of `DiagnosticAdapter` for JVM, Node and explicit unsupported/native capabilities. Function `validate_attribution(record: dict[str, object], artifact_root: Path) -> CriterionResult`.

- [ ] Write RED verification test:

```python
from nanolab.tasks.soak.diagnostics import gc_completed

def test_successful_gc_command_is_not_observed_collection():
    assert not gc_completed(4, 4, True)
    assert not gc_completed(None, None, True)
    assert gc_completed(4, 5, True)
```

- [ ] Implement checkpoint receipts with start/end, target identity, command/helper digest, completion evidence, exit status and artifact hashes. A counter increment proves a collection, not by itself the requested full collection: match the configured full-cycle/event evidence before labeling a full post-GC checkpoint valid.
- [ ] JVM adapter uses compatible jcmd/JFR/GC-log/heap diagnostics where available; a distroless image needs an explicitly provisioned compatible diagnostic helper with correct PID namespace, credentials and attachment permissions. Do not require a shell inside the target image. Node adapter uses a controlled inspector connection or explicit supported diagnostic endpoint, never a publicly exposed inspector by default. Separate helper resource usage from the target.
- [ ] Native adapter advertises only observed capabilities. Missing JVM metrics are not_applicable, but missing native-required evidence is unavailable and blocks P24. Do not change image/runtime silently to make diagnostics work.
- [ ] Enforce per-operation timeout, maximum dumps and disk limits before starting. Collect natural final evidence before forced diagnostics; mark perturbation windows and exclude them from ordinary latency/RSS recovery checks. Growth-triggered intrusive diagnostics occur after the relevant ordinary window or invalidate that window explicitly.
- [ ] Attribution record requires criterion ID, artifact hashes, owner, population, expected lifetime, remaining size/budget, rationale and reviewer identity. An attribution cannot waive a violated numerical limit or rewrite policy. Unresolved attribution stays INCONCLUSIVE.
- [ ] Run focused diagnostics tests including attach failure, wrong PID, unsupported runtime, command timeout, forced-GC mismatch and corrupted attribution artifact. Gate: no invented post-GC sample or automatic leak diagnosis from RSS alone.

## Task 6: Offline Evaluator and Honest Report

**Files:** Create `tasks/soak/evaluate.py`, `tasks/soak/report.py`, `tests/soak/test_evaluate.py`, `tests/soak/test_report.py`.

**Interfaces:** `combine_results(results: tuple[CriterionResult, ...], aborted: bool) -> Status`; `evaluate_run(run_dir: Path, attribution_path: Path | None = None) -> tuple[CriterionResult, ...]`; `write_report(run_dir: Path, results: tuple[CriterionResult, ...], aborted: bool) -> Path`.

- [ ] Write RED outcome precedence:

```python
from nanolab.tasks.soak.models import CriterionResult
from nanolab.tasks.soak.evaluate import combine_results

def test_known_failure_is_not_hidden_by_missing_metrics():
    results = (
        CriterionResult("retention", "FAIL", "expired payload retained", ("samples.jsonl",)),
        CriterionResult("rss", "INCONCLUSIVE", "missing sample", ()),
    )
    assert combine_results(results, aborted=False) == "FAIL"
    assert combine_results(results, aborted=True) == "ABORTED"
```

- [ ] Implement aggregation without losing individual failures:

```python
if aborted:
    return "ABORTED"
if any(item.status == "FAIL" for item in results):
    return "FAIL"
if not results or any(item.status != "PASS" for item in results):
    return "INCONCLUSIVE"
return "PASS"
```

- [ ] Evaluate required coverage/completeness before assigning a numerical PASS. Require enough samples in configured windows and enforce maximum observation gaps; one low final sample cannot hide a missing drain. Detect non-monotone phase timelines and process changes.
- [ ] Implement maximum, expected-zero, return-to-reference and growth-review operations. Growth-review compares configured equal-work windows/post-GC checkpoints using explicit thresholds and requests attribution, not automatic leak classification. Require owner/lifetime/budget policy for allowed plateaus; an expired payload violation is FAIL even with zero slope.
- [ ] Verify offered/admitted/success/error/retry/replay semantics per workload receipt. Dropped iterations make workload validity INCONCLUSIVE; actual observed correctness violations remain FAIL. Do not substitute admitted count with successful responses.
- [ ] Generate versioned JSON plus Markdown report with per-role graphs/tables using persisted samples; graph rendering failure must not destroy raw data or the numerical result. Show declared versus effective limits, natural versus diagnostic samples, missing coverage and next diagnostic action.
- [ ] Add tests for RSS above allowed residual despite released heap, plateau of expired payload, missing timer ownership, absent SDK metrics, label growth, unknown attribution, invalid timestamps and identical offline re-evaluation. Re-evaluation writes a new evaluation directory rather than replacing original evidence.
- [ ] Run focused evaluator/report tests. Gate: all four statuses and every policy boundary covered by positive and negative fixtures.

## Task 7: Constant Workload and Bounded Cancellation

**Files:** Create `tasks/soak/workload.py`, `tests/soak/test_workload.py`; create `packages/nanolab/assets/k6/soak-workload.js` only for behavior the existing mixed generator cannot express without comparison coupling.

**Interfaces:** `constant_arrival_options(rate: int, duration_s: int, vus: int) -> dict[str, object]`; workload adapter implements `WorkloadDriver` and writes a schema-versioned receipt with target request rates, actual counters, process exit and timing.

- [ ] Write RED workload shape:

```python
from nanolab.tasks.soak.workload import constant_arrival_options

def test_soak_is_constant_arrival_not_closed_loop():
    options = constant_arrival_options(100, 5400, 200)
    assert options["executor"] == "constant-arrival-rate"
    assert options["rate"] == 100
    assert options["duration"] == "5400s"
```

- [ ] Build options with timeUnit 1s and explicit allocated/max VUs, workload rate per function and payload hashes. Preserve actual HTTP/body correctness checks. For main P24 forbid idempotency keys and async share; prerequisite scripts may deliberately exercise them.
- [ ] Reuse the shared k6 executor only if its tested cancellation contract stops and reaps its child process group. Otherwise implement a narrowly scoped NanoLab driver using argv, role-bound execution and owned process lifetime; do not patch external Sonata packages silently.
- [ ] Test cancellation with a synthetic child/grandchild, graceful stop deadline then termination escalation restricted to owned children, idempotent stop and partial receipt persistence. Never kill by global process name. Record generator end time after outstanding requests finish or the bounded graceful-stop deadline expires; timeout/forced stop invalidates a normal completion claim.
- [ ] Record k6 threshold failure as evidence so drain/report can still execute. A driver crash takes the incomplete-run path but does not skip final observation.
- [ ] Run focused workload tests. Gate: no running generator survives normal completion/cancellation and no driver exit alone counts as P24 PASS.

## Task 8: Short-Profile Prerequisite Receipts

**Files:** Create `tasks/soak/prerequisites.py`, `tests/soak/test_prerequisites.py` and `packages/nanolab/scenarios-v2/memory-soak-prerequisites-container.yaml`.

**Interfaces:** `validate_receipt(receipt: dict[str, object], expected_fingerprint: str, required_coverage: frozenset[str]) -> CriterionResult`.

- [ ] Write RED mismatched-image receipt:

```python
from nanolab.tasks.soak.prerequisites import validate_receipt

def test_old_image_receipt_does_not_unlock_p24():
    result = validate_receipt(
        {"fingerprint": "old", "status": "PASS", "coverage": ["sync"]},
        "current", frozenset({"sync"}),
    )
    assert result.status == "INCONCLUSIVE"
```

- [ ] Define receipts with image identities for all roles, relevant config hashes, payload/script hash, coverage IDs, assertion results, timings and evidence hashes. Validate required artifact files and hashes; a caller-authored PASS string is insufficient.
- [ ] Implement independently reported checks for SYNC success, bounded error/timeout/cancellation, async completion/late callback, idempotent replay and function-name churn where required by selected P24 coverage. Reuse existing correctness/validation tasks through adapters; a check asserts final retained populations, not just request completion. Run destructive/churn checks in their own resource lifetime, not against the subsequent measured process.
- [ ] Support executing these checks first or accepting matching saved receipts. Preserve ownership distinctions between logical timeout and physical handler settlement. Record unsupported coverage as incomplete rather than deleting that requirement.
- [ ] Add positive receipt, tampering, SDK-only digest change, config mismatch, missing coverage and smoke-not-P24 tests. Gate: no long P24 starts on stale or partial prerequisite coverage.
- [ ] Run focused prerequisite tests and existing reused validation-task tests.

## Task 9: Sonata Lifecycle, Phase Ordering and Cleanup

**Files:** Create `tasks/soak/workflow.py`, `plans/soak.py`, `tests/plans/test_soak.py`, `tests/soak/test_lifecycle.py`.

**Interfaces:** `phase_order() -> tuple[str, ...]`; `build_soak_plan(config: ScenarioConfig, environment: EnvironmentConfig, bindings: RoleBindings, *, run_dir: Path, repo_root: Path, tool_root: Path) -> Workflow`.

- [ ] Write RED phase contract:

```python
from nanolab.tasks.soak.workflow import phase_order

def test_final_evidence_precedes_teardown():
    order = phase_order()
    assert order.index("drain") < order.index("final-diagnostics")
    assert order.index("report") < order.index("cleanup")
```

- [ ] Build the actual Sonata dependency graph using `add_platform` and existing isolated Compose resources. Require platform and function resources through final diagnostics/report, as the existing loadtest builder does for its composite. Test compiled resource dependencies, not only the phase_order helper.
- [ ] Implement `preflight -> prerequisites -> warmup -> baseline-drain -> baseline -> steady -> drain -> final-diagnostics -> evaluate -> report` inside one measurement lifetime. Expose phases through Sonata events/tasks without allowing selection to bypass their prerequisites. Declare a single observer resource spanning all measurement phases.
- [ ] Freeze a run ID and acquire exclusive ownership before platform creation. Reject a second run on an occupied shared endpoint; reuse an existing registry only under its actual shared-resource contract. Limit overrides belong to run-owned Compose configuration, not permanent NanoFaaS deployment edits.
- [ ] Ensure reporting/final bounded capture also executes after exceptions. Cleanup order: stop/reap generator, final bounded capture if possible, flush/stop observer, write partial verdict, release run-owned platform resources. Cleanup failure is secondary evidence, not a replacement for the original error.
- [ ] Add fault injection at every phase including setup before all resources exist, cancelled drain, disk-full report, sampler exception and diagnostic timeout. Fake clocks must exercise full 90+35 minute scheduling without real waits.
- [ ] Add lifecycle tests with actual Sonata compilation/execution using fake resources and drivers. Gate: no background observer/generator, orphan task or resource release before evidence persistence.

## Task 10: CLI/TUI Routing and Offline Evaluation

**Files:** Create `cli/soak.py`, `tests/test_soak_cli.py`; modify `cli/product.py`, `tui/app.py`.

**Interfaces:** `soak_exit_code(status: Status) -> int`; `validate_soak_selection(*, resume: bool, only: str | None, start: str | None, until: str | None) -> None`. Register `nanolab soak-evaluate RUN_DIR [--attribution PATH]` through the existing application command registration path.

- [ ] Write RED exit codes:

```python
import pytest
from nanolab.cli.soak import soak_exit_code

@pytest.mark.parametrize("status,expected", [
    ("PASS", 0), ("FAIL", 1), ("INCONCLUSIVE", 2), ("ABORTED", 130),
])
def test_exit_code_preserves_outcome(status, expected):
    assert soak_exit_code(status) == expected
```

- [ ] Route workflow soak explicitly before comparison/loadtest fallback in both CLI and TUI. Integrate scenario catalogue, inspect, tool checks, unique run directories and output links. No remote provisioning or build is allowed while rendering `plan`.
- [ ] Reject resume and partial task selection for executable soak; the full measurement lifetime is indivisible. Offline evaluation is the supported way to revisit artifacts. Integrate --keep with the existing retention mechanism and run-owned journal; teardown must release the selected run's retained resources, not globally named containers.
- [ ] Ensure generic run metadata does not report passed when soak status is INCONCLUSIVE or ABORTED. TUI and notification outcomes must use the same terminal status and distinguish smoke scope.
- [ ] Use Typer runner tests with fake builder/executor: plan has zero side effects, run invokes soak not comparison, interrupted run writes ABORTED, failed cleanup remains visible, offline evaluation never contacts Docker/HTTP, and occupied run directory is rejected.
- [ ] Run new CLI tests plus existing `tests/test_tui_workflow.py` and `tests/test_tui_workflow_controller.py`. Gate: same status across JSON, shell exit, CLI and TUI.

## Task 11: Presets, Migration and Operator Guide

**Files:** Modify `scenarios-v2/memory-soak-sync-container.yaml`, `scenarios-v2/memory-soak-sync-native.yaml` under `packages/nanolab/`; create smoke preset, `docs/soak.md`, `tests/soak/test_presets.py`; modify root `README.md`.

**Interfaces:** Public entry remains `nanolab plan SCENARIO`, `nanolab inspect SCENARIO`, `nanolab run SCENARIO`; new offline entry is from task 10.

- [ ] Write tests loading each shipped YAML through ScenarioConfig, checking workflow soak, unique criterion IDs, complete role policies and purpose labeling. Test that the native preset either has all required capability receipts or stops before workload with an explicit unsupported reason.
- [ ] Migrate historical steady/drain durations to 5400/2100 seconds and retention windows to 30/300/1800 seconds with 300-second cleanup margin, subject to effective runtime verification. Use explicit rates and image inputs, no embedded candidate/baseline naming or implicit SDK build. Require actual per-function resource policy as well as the 2 CPU/1024 MiB historical control-plane cap.
- [ ] Derive semantic zero/retention checks from verified publisher/owner contracts. Declare numerical memory policies with documented rationale before the first full run; do not infer them from the candidate's observed plateau. If existing evidence does not justify a residual budget, use an explicit operator-required policy file and reject execution without it rather than shipping a permissive default. The scenario schema and guide must identify this input and how inspect resolves it; implement resolution before Pydantic validation with paths relative to the scenario and record the resolved policy hash.
- [ ] Add smoke preset with reduced durations and purpose smoke; the report may say smoke checks PASS but must show `p24_qualified: false`. A smoke receipt cannot satisfy P24 prerequisites.
- [ ] Document diagnostic helper access, resource costs, required metrics, meaning of RSS/heap/cgroup, input policy files, unsupported native capabilities, cancellation, --keep/teardown and offline attribution. Explain migration from legacy soakMinutes/drainMinutes; retain legacy loadtest behavior for compatibility without describing it as the new P24 gate.
- [ ] Run preset/config tests and non-executing plan rendering only after authorization. Gate: reproducible commands and no fake thresholds or unused YAML knobs.

## Task 12: Integration Gate and First-Run Handoff

**Files:** Create `tests/soak/test_workflow_smoke.py`, `docs/soak-example-report.md`; use new modules and tests from tasks 1-11.

- [ ] Build a fake end-to-end fixture with two SDK roles, one control-plane role, a released-state trace and complete receipts. Run through real Sonata composition and the offline evaluator; assert report status PASS and all role coverage present.
- [ ] Mutate one fact per test: limit not applied, lost offered work, timer/payload retention after TTL, RSS above policy with clean heap, disappearing SDK scrape, label cardinality growth and unverified full GC. Assert FAIL or INCONCLUSIVE as defined, never PASS. Add interruption before and during drain with partial evidence and no surviving children.
- [ ] Run the full focused suite when authorized:

```bash
export NANOFAAS_ROOT=/home/michele/Documenti/nanofaas
uv run --locked --package nanolab pytest -c packages/nanolab/pyproject.toml --no-cov packages/nanolab/tests/soak packages/nanolab/tests/config/test_soak.py packages/nanolab/tests/plans/test_soak.py packages/nanolab/tests/test_soak_cli.py
```

- [ ] Run existing package regression/static gates when authorized; do not lower repository-wide coverage thresholds to accommodate focused tests:

```bash
uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
uv run --locked --all-packages --all-groups ruff check packages/nanolab
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```

- [ ] Obtain approval for the real smoke's exact images, resource budget and diagnostic access. Run smoke via the documented nanolab command, preserve artifacts and label the result as smoke, not P24 acceptance. Do not automatically chain the 125-minute experiment.
- [ ] Deliver exact long-run inspect/plan/run commands using frozen images containing the NanoFaaS fixes. List verified prerequisites, unresolved required instrumentation and final policy file. If a mandatory source is unavailable, mark readiness blocked on that source; do not weaken the contract to launch.
- [ ] After explicit approval for the long run, execute P24 and link its evidence in NanoFaaS without overwriting historical experiment records. A long-run finding goes back to the owner task and requires a repeat of the affected profile.

## Coverage and Checkpoints

| Spec section | Implementation tasks |
| --- | --- |
| 1-2: single-version purpose, scope, separate historical comparison | 1, 8, 11, 12 |
| 3: Sonata composition and separated responsibilities | 4, 6, 7, 9, 10 |
| 4: configuration, provenance and immutable policies | 1, 2, 3, 11 |
| 5: phases, retention-derived timing and prerequisite evidence | 1, 4, 8, 9 |
| 6: per-process memory, GC verification and attribution | 3, 4, 5, 6 |
| 7: honest acceptance and all terminal states | 6, 10, 12 |
| 8: incremental artifacts, cancellation, isolation and cleanup | 2, 7, 9, 10 |
| 9: unit/composition/negative tests and real smoke | Every task, final gate 12 |

Review checkpoints: tasks 1-3 lock the evidence contract; tasks 4-6 establish measurement and evaluation; tasks 7-10 complete execution lifetime and interfaces; tasks 11-12 deliver usable presets and readiness for the separately authorized long run. Each task has its own RED/GREEN cycle and can be rejected independently. No commit is implied by completing a checkpoint.

## Handoff Status

This document is an implementation plan only. No planned code has been implemented, no test listed here has been run, and no new soak has been started while writing it. Preserve existing NanoLab and NanoFaaS changes. Start with task 1 after selecting the execution method and satisfying repository permissions/impact requirements.

## Approved Amendment: Build Required Images from Source by Default

The user clarified during integration that the workflow must compile the images it needs. This amendment implements spec section 10 and supersedes prebuilt-only assumptions in tasks 3, 9, 11 and 12. It does not authorize an immediate expensive image build during the integration review.

### Task 1 contract addition

Add a strict `ImageBuildSpec` per application role to `SoakConfig.images`. Fields: `mode` (`build` by default or explicit `prebuilt`), `variant` (existing supported variant identifier), `platform`, `modules`, `build_options`, and optional `digest`/`provenance_receipt` required for prebuilt. Reject a missing role or contradictory build/prebuilt fields. Resolve variant recipes through the existing NanoLab catalogue; do not create a second variant catalogue. Built role images need no pre-existing tag input.

### Task 3 split: Build receipts before effective-runtime preflight

Create `packages/nanolab/src/nanolab/tasks/soak/images.py` and `packages/nanolab/tests/soak/test_images.py`. Inspect existing NanoLab image-build and source-snapshot primitives and run impact analysis before adapting them. Produce `BuildReceipt` values containing role, source snapshot fingerprint, recipe fingerprint, platform, toolchain identities, resolved base image identities, output digest and log paths.

- [ ] Write tests that build-mode planning requests a control-plane recipe and every selected function recipe, including JavaScript's SDK, without looking up existing application tags.
- [ ] Add the negative test that a requested native Java recipe cannot resolve to a JVM recipe, and that differing selected modules yield different recipe identities.
- [ ] Add tests that prebuilt mode requires digest/provenance and that failed builds never fall back to a local image carrying the expected tag.
- [ ] Implement `build_key(source: str, recipe: str, platform: str) -> str` using canonical JSON and SHA-256; test that changing source, recipe or platform changes the key independently.
- [ ] Capture one immutable source snapshot, including declared local modifications, before any image build. Reuse it across all role builds; preserve original source files and record submodule/generated inputs where the recipe consumes them.
- [ ] Execute existing build primitives as Sonata tasks before deployment, persist receipts and publish to unique run-owned tags. Resolve published digests and deploy those exact artifacts.
- [ ] Record effective toolchain/base identities rather than claiming lockfile or Dockerfile intent proves the identity. Reject missing required provenance before starting the measured workload.
- [ ] Test build cancellation and partial failure preserve logs and release only owned builders. Do not destroy shared build caches or registries.

Minimum independent build-key regression:

```python
from nanolab.tasks.soak.images import build_key

def test_build_key_does_not_reuse_a_different_sdk_snapshot():
    before = build_key("source-sdk-before", "javascript-recipe", "linux/amd64")
    after = build_key("source-sdk-after", "javascript-recipe", "linux/amd64")
    assert before != after
```

Run the new image unit tests with the same interpreter/environment and pytest configuration as task 1; no actual Docker build in those unit tests. Real image build integration belongs to the explicitly authorized real smoke.

### Task 9 ordering addition

Before runtime preflight, compose `build-preflight -> source-snapshot -> build-images -> publish-images -> freeze-digests -> deploy`. Platform build flags must be disabled after this preparation to prevent a second build from replacing measured artifacts. Observation of the measured process starts after build activity ends; compile resources are not counted as control-plane/SDK memory. Snapshot/registry/build resources remain governed by Sonata ownership and cancellation.

### Tasks 11 and 12 preset and acceptance changes

The JVM and native presets select build recipes for every role by default, not manually prepared image names. Preserve the imported `functionImages` support for explicit prebuilt legacy workflows, but do not use it to bypass the new build stage. Existing native preset compilation currently fails before any build: it reaches the comparison wrapper without a control-plane image and has an incomplete function image mapping. Replace that legacy routing through the dedicated soak workflow rather than papering over it with mandatory image tags.

Add an integration test proving a built artifact's digest reaches deployment unchanged and there are no build tasks after the freeze boundary. Extend plan side-effect tests to forbid actual Docker builds and source mutations. The real smoke must exercise the complete build-to-report path at least once before readiness for a P24 run is claimed.

### Integration checkpoint, 2026-09-13

Worktree `/tmp/nanolab-p24-soak`, branch `codex/p24-single-version-soak`, based on `f5fcf3c`. The three original source diffs and native preset were imported without conflicts; the original checkout was not modified. Existing targeted configuration/loadtest/comparison tests: 78 passed. New integration tests: three passed, one failed because the legacy native preset does not compile. That failure remains open pending the new build-aware path; no successful native run or completed merge is claimed. No commit or soak was performed.
