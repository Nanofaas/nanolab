# Native Kubernetes Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Qualify the native control plane through the existing Kubernetes
validation workflow and prevent silently validating a JVM image for a native request.

**Architecture:** Reuse frozen recipe builds, distribution metadata, Helm and
owned Kubernetes resources. Native checks select their behavior from the validated
distribution at runtime. Keep the existing queue test intact, then run a separate
native API/quota/runtime phase against the acquired deployment.

**Tech Stack:** Python 3.12, Sonata 0.6.14, existing HTTP/command tasks, Docker,
GraalVM recipe builds, Helm/Kubernetes, and Linux procfs.

**Spec:** [Approved first-delivery design](../specs/2026-10-08-native-k8s-lifecycle-design.md).

## Global Constraints

- No new workflow name, callback server, SDK matrix or native CLI belongs here.
- Freeze source outside the operator's NanoFaaS checkout; retain its revision.
- Reuse public Sonata dependencies and existing command deadline/output limits.
- Keep the ordinary Java JVM function workload fixed.
- Configure a 1-CPU limit without an explicit ioWorkerCount override.
- Arm64 qualification requires epoll and the actual chart-derived worker count.
- Remove only acquired resources; cleanup failure makes the run fail.
- Preserve the original 90% coverage threshold and report its existing debt.
- Real native build and Kubernetes evidence are required before closing #53.

## Review Focus

- A recipe changed after planning must not bypass native gates; derive the mode
  from the staged, validated distribution, not a cached YAML-mode boolean (Task 1/4).
- A replicas response for another function must fail despite HTTP 200 (Task 2).
- A queue-admission 429 must not satisfy the invocation-quota gate (Task 3).
- A replaced pod or recycled PID must invalidate thread evidence (Task 4).
- A diagnostic or quota failure must still release owned resources (Task 4/5).

---

### Task 1: Native preset and explicit selection

**Files:** Modify `packages/nanolab/src/nanolab/config/scenario.py`,
`packages/nanolab/src/nanolab/plans/validate.py`, and
`packages/nanolab/src/nanolab/tasks/platform.py`.
Create bundled `assets/presets/recipes/validate-k8s-native.yaml` and
`assets/presets/scenarios/deployment-lifecycle-k8s-native.yaml` beneath
`packages/nanolab/src/nanolab/`.
Test in `packages/nanolab/tests/plans/test_recipe_validate.py` and the existing
scenario/platform tests; document the preset in the existing recipes README.

**Interfaces:** Preserve `ScenarioConfig.validate_workflow()` and
`build_validate_plan(...) -> Workflow`. The existing recipe `resolved_spec(inputs)
inside `_helm_release_with_endpoint` consumes `RecipeDistribution.control_plane()`;
for native mode, replace the CPU-limit Helm value with `1` and clear any worker
override. Leave JVM values and the queue-capacity settings unchanged.

- [x] Write tests requiring legacy validate/native requests to raise before
  executor calls with a message pointing to `deployment-lifecycle-k8s-native.yaml`.
  Include direct builder calls with a constructed config, not only YAML loading.
- [x] Run those tests RED against the current ignored selector.
- [x] Add the guard and native recipe with modes `native`/`jvm`, modules
  `k8s-deployment-provider`, `build-metadata`, `sync-queue`, and a distinct native
  variant/tag. Remove JVM-only control-plane arguments.
- [x] In the recipe Helm acquire, derive native settings from the validated
  distribution, keeping unrelated provider/JVM behavior intact. Test that changing
  the staged mode changes the acquired Helm settings rather than skipping checks.
- [x] Verify GREEN with `pytest packages/nanolab/tests/plans/test_recipe_validate.py
  packages/nanolab/tests/tasks/recipes/test_validation.py
  packages/nanolab/tests/config/test_scenario.py --no-cov`.
  Commit `feat: add native Kubernetes lifecycle preset`.

### Task 2: Verify actual lifecycle API bodies

**Files:** Modify `packages/nanolab/src/nanolab/tasks/http_function.py`.
Create `packages/nanolab/src/nanolab/tasks/validation/native_kubernetes.py` and
`packages/nanolab/tests/tasks/validation/test_native_kubernetes.py`.
Test the shared PUT behavior in
`packages/nanolab/tests/tasks/functions/test_http_function.py`.

**Interfaces:** Preserve `HttpFunctionSetReplicasTask`'s constructor and validate
its JSON `function` and `replicas`. Define
`check_native_api(inputs: TaskInputs, *, function: PlatformFunction,
endpoint: Endpoint, executor: CommandTaskExecutor, role: ExecutionRole,
cwd: Path | None) -> None` in the new validation module.

- [x] Test malformed/empty/wrong-function PUT bodies and wrong replica values;
  run RED, then add the shared response verifier using the pinned API fields
  `function` and `replicas`, not the different GET-status field names.
- [x] Test register/patch/GET/PUT/invoke/delete against independent HTTP fixtures.
  Reject HTTP-200 error/timeout envelopes and a wrong function identity. Require
  the pinned delete contract's HTTP 204; an empty successful deletion is valid.
- [x] Implement the helper with existing HTTP tasks and bounded command transport.
  It receives the function with its image resolved from the validated distribution.
  Delete the already registered owned function, then require fresh registration
  HTTP 201 and its JSON body; a conflict/GET fallback cannot prove POST serialization.
  PATCH `concurrency` to 1 and `timeoutMs` to 5000, verifying the returned values.
  Re-register the same owned function after the strict DELETE check so subsequent
  quota checks can use it and existing compensated cleanup remains effective.
- [x] Verify GREEN with the two affected test modules. Preserve the distinction
  between strict lifecycle DELETE validation and best-effort cleanup DELETE.
  Commit `fix: validate native lifecycle response bodies`.

### Task 3: Independently exercise the invocation-quota envelope

**Files:** Extend the native validation module and its tests. Create
`packages/nanolab/src/nanolab/assets/diagnostics/native_quota_burst.py` and
`packages/nanolab/tests/tasks/validation/test_native_quota_burst.py`.

**Interfaces:** The diagnostic defines
`run_burst(url: str, function: str, payload: dict[str, object],
output: Path) -> None` and a CLI taking `--url`, `--function`, `--out`.
It uses standard-library HTTP and a 30-worker pool, retaining each response
separately. Its shipped payload repeats `alpha beta ` 25,000 times with `topN=3`,
bounded below the 1 MiB ingress limit, through the existing word-stats JVM artifact.

- [x] Write real local-HTTP tests where the independent fixture holds one request
  and rejects overlaps. Require at least one HTTP 429 with a JSON object whose
  `error` equals `invocation_quota_exceeded`; reject malformed bodies, only-queue
  rejections, no quota event, and accepted calls carrying error envelopes.
- [x] Run RED, implement 10s per-call timeouts, a 30s burst deadline, 64 KiB
  per-response bounds, a 4 MiB total receipt bound, and separate response
  records, then run GREEN. Missing overlap in a live burst is a qualification
  failure; do not manufacture a response or silently accept an inconclusive run.
- [x] Add the native quota phase after the existing synchronous queue burst:
  verify the acquired deployment identity, set only
  `NANOFAAS_INVOCATION_CAPACITY_EXECUTIONS_PER_FUNCTION=1` on that deployment,
  await rollout/readiness, and invoke the diagnostic against the owned function.
  Reuse existing asset staging for remote VM execution. Retain before/after pod
  identities and the capacity setting. Never change the operator's other releases.
- [x] Test ordering: ordinary queue validation precedes the quota setting;
  queue-depth 429 responses cannot qualify this new gate. Commit
  `feat: verify native invocation quota JSON`.

### Task 4: Runtime/log evidence and workflow integration

**Files:** Extend the native validation module. Modify
`packages/nanolab/src/nanolab/tasks/validation/workflow.py`.
Create `packages/nanolab/src/nanolab/assets/diagnostics/native_k8s_runtime.py` and
`packages/nanolab/tests/tasks/validation/test_native_k8s_runtime.py`.
Extend `packages/nanolab/tests/tasks/validation/test_workflow.py` and
`packages/nanolab/tests/tasks/recipes/test_kubernetes.py`.

**Interfaces:** Define `NativeKubernetesLifecycleTask(distribution:
Resource[RecipeDistribution], *, namespace: str, function: PlatformFunction,
endpoint: Endpoint, executor: CommandTaskExecutor, role: ExecutionRole,
run_dir: Path, function_component: tuple[str, str],
target: Resource[MinikubeTarget] | None = None)` returning
`TaskOutcome[None]`. It runs native gates only when the validated distribution's
control-plane mode is native; a JVM distribution never claims native qualification.
Define `verify_native_runtime(observation: dict[str, object], *,
expected_workers: int, require_epoll: bool) -> None` in the runtime diagnostic.

- [x] Write RED tests for missing/reflection-bearing logs, wrong native arguments,
  mismatched worker counts, arm64 nio, missing/ambiguous CRI process, and changed
  pod UID/container ID/PID start time. Include kernel-truncated Reactor names.
- [x] Implement the diagnostic: resolve the owned pod's immutable CRI container
  ID through node runtime inspection; record PID/start time before and after
  bounded procfs thread reads. Use Minikube SSH with the selected node locally;
  use existing stack-role k3s commands remotely. Reject an unresolved/mismatched
  node/container rather than reading an unrelated host process.
- [x] Implement the lifecycle task, retaining logs before and after the quota
  rollout. Resolve the helper's function image using `function_component` and the
  validated distribution, never a catalog's mutable default image. Bound every
  diagnostic command to 60s, logs to 8 MiB, and runtime JSON to 1 MiB; fail on
  oversize/truncated evidence, with a maximum 4,096 inspected threads.
  Re-run existing metadata/image checks on the resulting pod and verify
  its native `-Dreactor.netty.ioWorkerCount=1` argument and actual worker count.
  Drive traffic before sampling; missing evidence fails. Read logs only from pods
  selected through the deployment's owner chain.
- [x] Add the task after normal recipe validation/queue tasks with dependencies
  on the acquired platform, registered function, validated distribution and
  selected target. No planning-time native flag may disable runtime native gates.
- [x] Verify GREEN for native/JVM branching, the actual compilation graph and
  failure compensation. Inject API/quota/probe failures and require release of
  only run-owned resources. Commit `feat: qualify native Kubernetes runtime`.

### Task 5: Installed artifacts, live qualification and final review

- [x] Build/install an isolated NanoLab wheel and load the two bundled native
  presets through the installed CLI. Keep the original dependency lock unchanged.
- [x] Freeze an isolated NanoFaaS checkout with the native fixes, record its
  revision, and build the actual native recipe with existing memory controls.
- [x] Run the installed Kubernetes native lifecycle in an owned local Minikube
  target; collect actual metadata/image, API, quota, logs, arguments and thread
  evidence. Check normal cleanup. Missing prerequisites or a failed gate remain
  explicitly incomplete and prevent closing #53.
- [x] Run NanoLab and toolkit suites with their original coverage configurations
  outside the sandbox where asynchronous/local-HTTP tests require it, then quality
  hooks and dependency checks. Report the existing coverage debt separately.
- [x] Use the execution method selected by the user and obtain the required
  independent review. Fix material findings and verify the affected behavior.
  Commit the final evidence; no public image push or unrelated deployment.

## Self-review and handoff

All approved-spec requirements map to Tasks 1–5. The Review Focus cases each have
a test owner. Selection is based on the frozen distribution at execution time;
quota mutation happens only after existing queue validation; native/JVM metrics
are not compared. CLI and SDK/watchdog contracts remain later deliveries.

Recommended execution: I implement the tasks in this session, followed by one
independent whole-branch review. The tasks share one lifecycle and execution
interfaces; separate implementers would add handoffs without independent modules.
The user approved execution on `fix/operational-validation`. All five tasks and
the installed native lifecycle are complete. The independent review's two
material findings were fixed with RED-to-GREEN regressions, followed by the
full functional suite and a successful installed lifecycle rerun. The original
90% coverage gate remains failing at 86.31%; no threshold was relaxed.
The complete verification, review and execution decisions are tracked in
`2026-10-08-native-k8s-lifecycle-evidence.md`.
