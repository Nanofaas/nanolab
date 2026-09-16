# Control-plane Heap Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a separate NanoLab `heap-analysis` workflow that captures comparable post-GC control-plane HPROF files and produces standard headless MAT individual and comparison reports.

**Architecture:** Add a strict, control-plane-only configuration and a dedicated Sonata lifecycle. Reuse the existing single-version image/deployment, k6 workload, JVM diagnostic, artifact, ownership, and cleanup primitives, but do not invoke the complete soak lifecycle or its P24 acceptance policy. Extend the existing digest-pinned diagnostic helper with Eclipse MAT 1.17.0 and run MAT only after application teardown.

**Tech Stack:** Python 3.12+, Pydantic, Sonata Engine, Docker Compose, Docker Engine, k6 0.49, JDK 25 `jcmd`, Eclipse MAT 1.17.0 headless launcher, pytest.

**Spec:** `docs/superpowers/specs/2026-09-15-control-plane-heap-analysis-design.md`

## Global Constraints

- Analyze only the JVM control plane; the Java and JavaScript functions are workload destinations, not dump targets.
- Use one NanoFaaS source snapshot and build every application image before deployment.
- Run 120 seconds warm-up, 900 seconds measured load at 200 requests per second, and 420 seconds natural drain in the checked-in scenario.
- Capture the natural drain checkpoint before the final diagnostic GC.
- Capture exactly two post-full-GC control-plane dumps.
- Run MAT only after the application deployment has been released.
- Require digest-pinned helper and application images at deployment time.
- Do not add NanoFaaS instrumentation or modify the NanoFaaS repository.
- Do not reuse P24 criteria, retention gates, policy files, or `create_soak_lifecycle`.
- Do not implement a custom leak classifier or parse MAT HTML into a proprietary model.
- Treat HPROF and generated MAT reports as sensitive artifacts.

---

## File map

- Create `packages/nanolab/src/nanolab/config/heap_analysis.py`: strict public protocol model.
- Modify `packages/nanolab/src/nanolab/config/scenario.py`: register `heap-analysis` and validate its top-level shape.
- Create `packages/nanolab/src/nanolab/tasks/heap_analysis/mat.py`: bounded MAT container command and report manifest.
- Create `packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py`: ordered diagnostic lifecycle and terminal report.
- Create `packages/nanolab/src/nanolab/tasks/heap_analysis/__init__.py`: package marker and public task exports.
- Create `packages/nanolab/src/nanolab/plans/heap_analysis.py`: compile the Sonata workflow.
- Modify `packages/nanolab/src/nanolab/cli/product.py`: plan/run dispatch, local prerequisites, run directory, and terminal status output.
- Modify `packages/nanolab/assets/soak/diagnostic-helper.Dockerfile`: install the pinned MAT archive with a verified SHA-256.
- Create `packages/nanolab/assets/soak/mat-worker.py`: invoke only the approved MAT reports and copy bounded outputs.
- Create `packages/nanolab/assets/soak/mat.lock.json`: official MAT URL, version, and archive SHA-256.
- Create `packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml`: public preset.
- Create focused tests under `packages/nanolab/tests/config/`, `packages/nanolab/tests/heap_analysis/`, and `packages/nanolab/tests/plans/`.
- Create `docs/heap-analysis.md` and modify `README.md`: operator contract and command.

### Task 1: Configuration and scenario contract

**Files:**
- Create: `packages/nanolab/src/nanolab/config/heap_analysis.py`
- Modify: `packages/nanolab/src/nanolab/config/scenario.py`
- Test: `packages/nanolab/tests/config/test_heap_analysis.py`

**Interfaces:**
- Consumes: `ImageBuildSpec`, `RolePolicy`, and `WorkloadConfig` from `nanolab.config.soak` so build and workload inputs keep one representation.
- Produces: `HeapAnalysisConfig`, `ScenarioConfig.heap_analysis`, and workflow literal `heap-analysis` for Tasks 3 and 4.

- [ ] **Step 1: Write failing model tests**

Create tests around a minimal valid payload and mutate one property per negative case:

```python
def test_control_plane_heap_analysis_is_valid():
    config = HeapAnalysisConfig.model_validate(valid_heap_analysis())
    assert config.target == "control-plane"
    assert config.workload.rates == {
        "word-stats-java": 100,
        "word-stats-javascript": 100,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("target", "word-stats-java", "target must be control-plane"),
        ("max_dumps", 3, "exactly two dumps"),
        ("helper_image", "nanolab/helper:latest", "digest-pinned"),
    ],
)
def test_heap_analysis_rejects_unsupported_scope(field, value, message):
    raw = valid_heap_analysis()
    raw[field] = value
    with pytest.raises(ValidationError, match=message):
        HeapAnalysisConfig.model_validate(raw)
```

Add scenario tests proving `workflow: heap-analysis` requires `backend: container`, a `heapAnalysis` block, a JVM control-plane role, and resource keys limited to selected functions plus `control-plane`. Prove that a non-heap workflow rejects `heapAnalysis`.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
pytest -q packages/nanolab/tests/config/test_heap_analysis.py
```

Expected: collection fails because `nanolab.config.heap_analysis` does not exist.

- [ ] **Step 3: Implement the strict model**

Use the existing soak child models rather than copying image, role, and workload schemas:

```python
class HeapAnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["control-plane"] = "control-plane"
    warmup_s: int = Field(gt=0)
    steady_s: int = Field(gt=0)
    drain_s: int = Field(gt=0)
    roles: dict[str, RolePolicy]
    images: dict[str, ImageBuildSpec]
    workload: WorkloadConfig
    helper_image: str
    max_dumps: Literal[2] = 2
    max_dump_bytes: int = Field(gt=0)
    artifact_limit_bytes: int = Field(gt=0)
    diagnostic_timeout_s: int = Field(gt=0)
    mat_memory_mib: int = Field(gt=0)
    mat_cpus: float = Field(gt=0)
    mat_timeout_s: int = Field(gt=0)
```

The model validator must require matching role/image keys, `roles["control-plane"].runtime == "jvm"`, exactly two functions in `workload.rates`, positive rates, and `repository@sha256:<64 lowercase hex>` for `helper_image`. Keep generic positive timings and rates in the model; the exact 200 requests per second belongs only in the checked-in preset test.

Add `heap-analysis` to `WorkflowName`, add `heap_analysis` with alias `heapAnalysis` to `ScenarioConfig`, and give it a dedicated early-return validator branch parallel to the existing soak branch. Do not route it through `loadtest` validation.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run:

```bash
pytest -q packages/nanolab/tests/config/test_heap_analysis.py
```

Expected: all configuration tests pass.

- [ ] **Step 5: Commit the configuration contract**

```bash
git add packages/nanolab/src/nanolab/config/heap_analysis.py \
  packages/nanolab/src/nanolab/config/scenario.py \
  packages/nanolab/tests/config/test_heap_analysis.py
git commit -m "Add heap analysis configuration"
```

### Task 2: Pinned MAT helper and bounded report runner

**Files:**
- Modify: `packages/nanolab/assets/soak/diagnostic-helper.Dockerfile`
- Create: `packages/nanolab/assets/soak/mat-worker.py`
- Create: `packages/nanolab/assets/soak/mat.lock.json`
- Test: `packages/nanolab/tests/heap_analysis/test_mat_worker.py`
- Test: `packages/nanolab/tests/heap_analysis/test_mat.py`
- Create: `packages/nanolab/src/nanolab/tasks/heap_analysis/mat.py`

**Interfaces:**
- Consumes: baseline/final HPROF paths, a digest-pinned helper image, finite CPU/memory/time/output limits, and `OwnedCommandRunner` behavior already used by diagnostics.
- Produces: `MatAnalysisRequest`, `MatAnalyzer.run(request) -> Path`, and `analysis/manifest.json` containing commands, versions, dump hashes, report hashes, sizes, duration, and status.

- [ ] **Step 1: Write failing worker and command tests**

Test the worker with a fake `ParseHeapDump.sh` executable that records argv and creates named ZIP outputs. Assert these exact report invocations:

```python
expected = [
    ("baseline.hprof", "org.eclipse.mat.api:overview"),
    ("baseline.hprof", "org.eclipse.mat.api:suspects"),
    ("baseline.hprof", "org.eclipse.mat.api:top_components"),
    ("final.hprof", "org.eclipse.mat.api:overview"),
    ("final.hprof", "org.eclipse.mat.api:suspects"),
    ("final.hprof", "org.eclipse.mat.api:top_components"),
    (
        "final.hprof",
        "-snapshot2=baseline.hprof",
        "org.eclipse.mat.api:compare",
    ),
    (
        "final.hprof",
        "-baseline=baseline.hprof",
        "org.eclipse.mat.api:suspects2",
    ),
]
```

Test `MatAnalyzer` argv for `--network none`, `--read-only`, `--cpus`, `--memory`, read-only dump mounts, a bounded tmpfs work mount, and an owned read-write output mount. Test rejection of a tag, missing dump, oversized dump pair, symlinked dump, and reports that exceed the output budget.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
pytest -q packages/nanolab/tests/heap_analysis/test_mat_worker.py \
  packages/nanolab/tests/heap_analysis/test_mat.py
```

Expected: collection fails because the worker and `mat.py` do not exist.

- [ ] **Step 3: Freeze the official MAT archive metadata**

Use the released Linux AArch64 package, not a snapshot:

```bash
url=https://download.eclipse.org/mat/1.17.0/rcp/MemoryAnalyzer-1.17.0.20260601-linux.gtk.aarch64.zip
curl -fL "$url" -o /tmp/MemoryAnalyzer-1.17.0.20260601-linux.gtk.aarch64.zip
python3 -c 'import hashlib,json,pathlib; p=pathlib.Path("/tmp/MemoryAnalyzer-1.17.0.20260601-linux.gtk.aarch64.zip"); print(json.dumps({"version":"1.17.0.20260601","url":"https://download.eclipse.org/mat/1.17.0/rcp/MemoryAnalyzer-1.17.0.20260601-linux.gtk.aarch64.zip","sha256":hashlib.file_digest(p.open("rb"),"sha256").hexdigest()}, indent=2))' \
  > packages/nanolab/assets/soak/mat.lock.json
```

The build must read these values as explicit build arguments and verify the downloaded bytes before extraction. Do not commit the 95 MB ZIP.

- [ ] **Step 4: Extend the existing helper image minimally**

Add `MAT_URL` and `MAT_SHA256` build arguments. Download with Python's standard library, verify SHA-256, extract with `zipfile`, and copy `mat-worker.py`. Do not add curl, unzip, a package manager, or a second runtime image.

The resulting image must expose:

```text
/opt/mat/ParseHeapDump.sh
/opt/nanolab/mat-worker.py
```

Retain the current diagnostic-worker entrypoint so existing soak diagnostics are unchanged.

- [ ] **Step 5: Implement the worker and Docker runner**

`mat-worker.py` accepts exactly four positional paths: baseline HPROF, final HPROF, output directory, and MAT launcher. It copies both read-only inputs into its bounded work directory, runs the eight commands above with `subprocess.run(..., check=True)`, copies only ZIP/TXT/log outputs to the mounted result directory, writes `mat-worker-receipt.json`, and removes working copies in `finally`.

`MatAnalyzer.run()` uses the existing owned Docker command abstraction and writes the final manifest atomically. Status is `PASS` only when every required report exists, is non-empty, remains within the artifact budget, and hashes successfully.

- [ ] **Step 6: Run the focused tests and confirm GREEN**

Run:

```bash
pytest -q packages/nanolab/tests/heap_analysis/test_mat_worker.py \
  packages/nanolab/tests/heap_analysis/test_mat.py
```

Expected: all MAT unit tests pass without Docker or a real HPROF.

- [ ] **Step 7: Commit the MAT runner**

```bash
git add packages/nanolab/assets/soak/diagnostic-helper.Dockerfile \
  packages/nanolab/assets/soak/mat-worker.py \
  packages/nanolab/assets/soak/mat.lock.json \
  packages/nanolab/src/nanolab/tasks/heap_analysis/mat.py \
  packages/nanolab/tests/heap_analysis/test_mat.py \
  packages/nanolab/tests/heap_analysis/test_mat_worker.py
git commit -m "Add bounded MAT report runner"
```

### Task 3: Ordered Sonata heap-analysis lifecycle

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/heap_analysis/__init__.py`
- Create: `packages/nanolab/src/nanolab/tasks/heap_analysis/runtime.py`
- Test: `packages/nanolab/tests/heap_analysis/test_runtime.py`

**Interfaces:**
- Consumes: `HeapAnalysisConfig`, a frozen owned deployment, existing k6 workload producer, `JvmDiagnosticAdapter`, runtime observation collector, and `MatAnalyzer`.
- Produces: `RunControlPlaneHeapAnalysis(Task)`, `HeapAnalysisResult`, `report.json`, and the artifact tree specified by the design.

- [ ] **Step 1: Write failing lifecycle-order tests**

Use fakes that append events to one list and assert the exact safety order:

```python
assert events == [
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
```

Add tests proving an interrupted load stops the generator and releases the deployment, a dump/MAT infrastructure failure yields `INCONCLUSIVE`, a workload error yields `FAIL` after best-effort evidence capture, and MAT never starts while the deployment resource is held.

- [ ] **Step 2: Run lifecycle tests and confirm RED**

Run:

```bash
pytest -q packages/nanolab/tests/heap_analysis/test_runtime.py
```

Expected: collection fails because `nanolab.tasks.heap_analysis.runtime` does not exist.

- [ ] **Step 3: Implement the smallest dedicated lifecycle**

Create an explicit result type:

```python
@dataclass(frozen=True)
class HeapAnalysisResult:
    status: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    report: Path
    baseline_dump: Path | None
    final_dump: Path | None
    analysis_manifest: Path | None
```

`RunControlPlaneHeapAnalysis` must compose existing low-level primitives, not duplicate their subprocess logic. Reuse source/build/digest/deployment preparation and `compose_frozen_soak_workflow(..., workflow_id="heap-analysis")` only as deployment composition; provide a new measurement task and never call `create_soak_lifecycle`.

The measurement task performs warm-up, baseline observation/GC/dump, steady load, natural drain observation, final GC/dump, and post-GC observation. It returns dump paths and workload status. Make MAT a dependent Sonata task that requires the measurement result but not the deployment resources; this resource boundary enforces deployment release before analysis rather than relying on call order alone.

Write JSON artifacts through the existing bounded artifact writer. The terminal `report.json` must say that `PASS` means complete diagnostics, not no leak and not P24 qualification.

- [ ] **Step 4: Run lifecycle tests and confirm GREEN**

Run:

```bash
pytest -q packages/nanolab/tests/heap_analysis/test_runtime.py
```

Expected: all lifecycle tests pass.

- [ ] **Step 5: Run adjacent reused-component tests**

Run:

```bash
pytest -q packages/nanolab/tests/soak/test_diagnostics.py \
  packages/nanolab/tests/soak/test_diagnostic_exec.py \
  packages/nanolab/tests/soak/test_workload.py \
  packages/nanolab/tests/soak/test_teardown.py
```

Expected: existing soak behavior remains green.

- [ ] **Step 6: Commit the lifecycle**

```bash
git add packages/nanolab/src/nanolab/tasks/heap_analysis \
  packages/nanolab/tests/heap_analysis/test_runtime.py
git commit -m "Add control-plane heap analysis lifecycle"
```

### Task 4: Plan, CLI, preset, and operator documentation

**Files:**
- Create: `packages/nanolab/src/nanolab/plans/heap_analysis.py`
- Modify: `packages/nanolab/src/nanolab/cli/product.py`
- Create: `packages/nanolab/tests/plans/test_heap_analysis.py`
- Modify: `packages/nanolab/tests/test_soak_cli.py`
- Create: `packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml`
- Create: `packages/nanolab/tests/heap_analysis/test_preset.py`
- Create: `docs/heap-analysis.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: `RunControlPlaneHeapAnalysis`, `HeapAnalysisConfig`, environment role bindings, NanoFaaS/tool roots, and the real digest-pinned helper image produced from Task 2.
- Produces: inspectable/plannable/runnable `workflow: heap-analysis`, unique `heap-analysis-<uuid>` run directories, terminal status output, and the documented public command.

- [ ] **Step 1: Write failing plan, CLI, and preset tests**

Assert `_workflow()` dispatches to `build_heap_analysis_plan`, rejects non-local environments and endpoint overrides, requires `docker` and `k6`, and allocates a unique run directory. Assert `plan` performs no Docker/build/network action.

The preset test must assert:

```python
assert config.workflow == "heap-analysis"
assert config.heap_analysis.target == "control-plane"
assert config.heap_analysis.warmup_s == 120
assert config.heap_analysis.steady_s == 900
assert config.heap_analysis.drain_s == 420
assert sum(config.heap_analysis.workload.rates.values()) == 200
assert config.heap_analysis.max_dumps == 2
```

- [ ] **Step 2: Run integration tests and confirm RED**

Run:

```bash
pytest -q packages/nanolab/tests/plans/test_heap_analysis.py \
  packages/nanolab/tests/heap_analysis/test_preset.py \
  packages/nanolab/tests/test_soak_cli.py
```

Expected: plan import and workflow dispatch fail.

- [ ] **Step 3: Implement plan and CLI dispatch**

Expose:

```python
def build_heap_analysis_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    run_dir: Path,
    repo_root: Path,
    tool_root: Path,
) -> Workflow: ...
```

Mirror only the local owned-endpoint behavior needed from soak. Add `heap-analysis` beside soak in run-directory allocation, executable checks, endpoint override rejection, terminal status rendering, and exit-code mapping. Do not add teardown/resume CLI modes in this version; cleanup is automatic and `--keep` is unsupported because MAT requires deployment release.

- [ ] **Step 4: Build and pin the extended helper image**

Read `mat.lock.json` and pass its URL/SHA-256 as build arguments. Use the existing approved BuildKit helper recipe and resource limits. Publish to the local registry, resolve the resulting RepoDigest, and write that immutable digest into the preset. Never check in a mutable tag.

- [ ] **Step 5: Add the preset**

Copy only the role/image/workload portions required from the current control-plane soak preset. Set `workflow: heap-analysis`, omit `soakPolicyFile`, omit P24 criteria/retention/prerequisites, and set the exact durations and limits from the specification.

- [ ] **Step 6: Document operation and interpretation**

Document the public command, expected 24-minute application timeline plus MAT time, resource bounds, sensitive-artifact warning, artifact tree, and the distinction between diagnostic `PASS`, a MAT suspect, a Java leak, and P24 qualification. Link to the official MAT batch-mode documentation for `compare` and `suspects2`.

- [ ] **Step 7: Run integration tests and confirm GREEN**

Run:

```bash
pytest -q packages/nanolab/tests/plans/test_heap_analysis.py \
  packages/nanolab/tests/heap_analysis/test_preset.py \
  packages/nanolab/tests/test_soak_cli.py
```

Expected: all plan, preset, and CLI tests pass.

- [ ] **Step 8: Run the complete NanoLab unit suite**

Run:

```bash
pytest -q packages/nanolab/tests
```

Expected: all tests pass.

- [ ] **Step 9: Inspect and plan without executing**

Run:

```bash
./nanolab.sh inspect packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
./nanolab.sh plan packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
```

Expected: the resolved workflow shows one JVM dump target, two dumps, 200 requests per second, a digest-pinned helper, bounded MAT resources, and no P24 policy or criteria.

- [ ] **Step 10: Commit public integration**

```bash
git add packages/nanolab/src/nanolab/plans/heap_analysis.py \
  packages/nanolab/src/nanolab/cli/product.py \
  packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml \
  packages/nanolab/tests/plans/test_heap_analysis.py \
  packages/nanolab/tests/heap_analysis/test_preset.py \
  packages/nanolab/tests/test_soak_cli.py \
  docs/heap-analysis.md README.md \
  docs/superpowers/specs/2026-09-15-control-plane-heap-analysis-design.md \
  docs/superpowers/plans/2026-09-15-control-plane-heap-analysis.md
git commit -m "Add control-plane heap analysis workflow"
```

### Task 5: Real smoke and evidence review

**Files:**
- Modify only if the smoke exposes a demonstrated defect in files introduced by Tasks 1-4.
- Evidence: `packages/nanolab/runs/heap-analysis-*/`

**Interfaces:**
- Consumes: the checked-in scenario, NanoFaaS checkout, pinned helper image, Docker, and k6.
- Produces: one complete diagnostic receipt with baseline/final dumps and MAT individual/comparison reports.

- [ ] **Step 1: Run the real workflow**

Run:

```bash
NANOFAAS_ROOT=/home/michele/Documenti/nanofaas \
  ./nanolab.sh run \
  packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
```

Expected: 180,000 offered requests over 900 seconds, zero request failures, two non-empty HPROF files, required MAT reports, deployment release before MAT, terminal `PASS`, and no surviving helper/application containers.

- [ ] **Step 2: Review the generated receipt**

Confirm `analysis/manifest.json` binds every report hash to the two dump hashes, `report.json` labels artifacts sensitive, and the comparison report uses final as subject and baseline as reference. Record runtime, dump sizes, report sizes, top MAT suspects, and any residual risk in the task report.

- [ ] **Step 3: Commit only demonstrated smoke fixes**

If no defect was found, do not create a commit. If a defect was demonstrated, add the smallest RED/GREEN regression test, fix it, rerun that focused test, and commit only those files with an imperative message describing the defect.

## Completion gate

Before claiming completion:

```bash
pytest -q packages/nanolab/tests
./nanolab.sh inspect packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
./nanolab.sh plan packages/nanolab/scenarios-v2/memory-heap-analysis-control-plane-container.yaml
```

The real smoke receipt must also exist and show cleanup. Do not claim that MAT output proves a leak; report its dominators and retained-size deltas as evidence for review.
