# Module-cost workflow implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan
> task by task, followed by an independent review of the complete change.

**Goal:** Measure the cost of retaining inactive adaptation modules versus
excluding them, with identical service policies in JVM and native deployments.

**Architecture:** A dedicated NanoLab scenario composes existing Sonata recipe,
platform, provisioning and collection resources. A small preparer emits three
block inputs and ordinary NanoLab commands; offline Python produces verified
contrasts. No new provisioning engine or controller implementation.

**Tech stack:** Existing NanoLab Python/Pydantic, Sonata, NanoFaaS Gradle recipes,
k6, Kubernetes/Prometheus and the paper's Python analysis environment.

**Spec:** [Design](../specs/2026-11-01-module-exclusion-design.md).

Paths below are relative to the NanoLab repository except the final paper task.
This is a proposed implementation plan; it does not authorize a VM launch.

## Global constraints

- Base modules: k8s-deployment-provider, async-queue, build-metadata.
- Four module sets times JVM/native; one function, fixed concurrency, one replica.
- FIXED/NONE disable adaptation, not module initialization or periodic work.
- Keep missing telemetry, failed builds, evictions and failed attempts explicit.
- Azure Spot VMs are workflow-created; controller results remain under /data.
- No additional build CPU/RAM caps; no heap below 64 MiB.
- Pin recipes, source identities, effective build options and function image digest.
- Numerical defaults in the spec are proposed settings to freeze in the manifest.

## Review focus

- A selected native module may be removed by AOT: require runtime corroboration.
- Different queues/replica counts/concurrency invalidate the intended contrast.
- Build workers and image pulls must not enter readiness or idle measurements.
- Dropped/missing requests must not create apparent efficiency improvements.
- Replacement Spot hosts and interrupted attempts must not silently form pairs.

### Task 1: strict protocol, recipe matrix and preparation

Files: add `config/module_cost.py`, extend `config/scenario.py`, add
`comparison/module_cost.py`, `scripts/module-cost-prepare.py`; tests under
`tests/config/test_module_cost.py` and `tests/comparison/test_module_cost.py`.

Interface: `ModuleCostConfig` contains one block identity and frozen arm order;
`module_cost_cells(...)` returns the eight explicit runtime/module selections.
The preparer writes validated scenarios/environments/recipes and source hashes.
Reuse existing recipe validation and `comparison.cells` identity helpers.

- [x] Test exact eight-cell coverage, shared base modules, distinct image identity,
      explicit 64 MiB heap settings, malformed order and ignored-option rejection.
- [x] Run these tests red; implement only the necessary model/catalogue/preparer.
- [x] Verify generated configs through actual NanoLab validators; test generated
      shell quoting with a fake executable, ensuring preparation never provisions.
- [x] Commit the independently usable preparation change.

### Task 2: recipe-backed workflow and owned infrastructure

Files: add `plans/module_cost.py`; wire `plans/dispatch.py` and
`application/provisioning.py`; add a canonical scenario preset in
`assets/presets/scenarios/`; test `tests/plans/test_module_cost.py` and provisioning.

Interface: `build_module_cost_plan(config, environment, bindings, *, run_dir,
repo_root, fetcher, prometheus_client)` returns the composed Sonata Workflow.
Reuse `tasks/recipes/workflow.py` distribution resources and `RecipeBinding`,
`PlatformRequest`/`add_platform`, and the existing stack/loadgen provider lifecycle.

- [x] Test planning does not build or allocate; reject local/shared generators,
      stale results, missing corpus/oracle and non-Spot managed environments.
- [x] Assert every registration has the same function digest, limits, queue,
      concurrency and NONE policy; only selected adaptation modules/runtime vary.
- [x] Build before measurement; test no build occurs between the launch boundary
      and sampled phases. Use fresh processes/pods per arm and scoped cleanup.
- [x] Test failure paths collect available evidence before resource release;
      replacement attempts preserve the original block and receipts.
- [x] Run focused regressions and commit.

### Task 3: measurement and evidence contract

Files: add `tasks/module_cost.py`, `assets/k6/module-cost.js` and a bounded remote
observation helper only where existing collectors are insufficient; tests under
`tests/tasks/test_module_cost.py` and `tests/tasks/test_module_cost_k6.py`.
Reuse `RunK6Task`, `FetchVmResults`, Prometheus snapshot collection,
`idle_reactivation.parse_observation` and `idle_reactivation_report.memory_window`.

B5's `assets/k6/function-runtime.js` currently hardcodes its load shape and logs
only response-end events. Do not silently use it unchanged for this protocol.
Extract only the shared exact-oracle/corpus functions into a small k6 helper,
keeping B5's emitted schema/behaviour unchanged; the new script owns the fixed
calendar, send/terminal ledger and whole-response timing. Reuse existing ledger
reconciliation where its schema fits, not its workload-specific acceptance rules.

- [x] Tests reproduce malformed/stale corpus, invalid HTTP-200 output, interrupted
      requests, missing metrics, reset counters, pod changes and changed limits.
- [x] Record artifact sizes, cached-image launch/readiness boundaries, warmup,
      drained idle interval and fixed-load cohort; keep clock domains explicit.
- [x] Test idle windows contain no outstanding warmup requests. Never force GC.
- [x] Capture positive retained-module initialization evidence and selected-module
      reports; absent series alone cannot prove exclusion or zero CPU/memory.
- [x] Exercise the real k6 script against a local HTTP fixture, including failures
      and scheduler boundary arrivals, before claiming the parser is compatible.
- [x] Run B5/B6 regressions for reused helpers and commit.

### Task 4: offline verification, contrasts and paper artifacts

Files: add `tasks/module_cost_report.py`, tests in
`tests/tasks/test_module_cost_report.py`; paper-side analysis/rendering scripts
under a dedicated `experiments/module-cost/` directory.

Interface: `analyze_module_cost(block_dir: Path)` writes per-arm results and
within-block/runtime contrasts from archived evidence. Paper scripts aggregate
only compatible complete blocks and generate numerical tables/figures.

- [x] Test time-weighted RSS/working-set integrals, CPU-counter deltas, units,
      incomplete coverage and the sign of C-Base, S-Base, CS-Base, CS-C-S+Base.
- [x] Test mixed host/session/source identities cannot be silently paired; retain
      measured but non-comparable attempts without manufacturing a result.
- [x] Independently recompute counts and resource integrals before generating
      LaTeX claims. Show individual block contrasts; requests are not replicates.
- [x] Verify regeneration from compressed archives and their member hashes.
- [x] Document commands, exact boundaries and unsupported/native-eliminated cases.
- [x] Run focused tests, lint/types and Linux CI, then obtain independent review.
      See `docs/module-cost-verification.md` for the full run and focused rechecks.

## Completion boundary

Implementation completion means a reviewed workflow, offline/fixture verification
and a concrete frozen execution manifest. Physical operation still needs one
qualification run to check module retention, fixed policies and measurement
coverage; this is not an automatic rate-sweep calibration. No cloud run, merge
or publication is implied by completing this plan.
