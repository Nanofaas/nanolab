# Idle Reactivation Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task.

**Goal:** Add a dedicated B6 workflow that measures paired ready/zero lifecycle costs without changing loadtest behavior.

**Architecture:** Register `idle-reactivation` in the existing scenario/dispatch/CLI machinery. Compose two sequential deployments with `add_platform`, building once and releasing each arm after its last consumer. Reuse function resolution, Helm values, role executors, provisioning, Prometheus collection and file fetching. New code owns only the sparse-request protocol, lifecycle qualification and paired evidence.

**Tech Stack:** Python, Pydantic, Sonata, Kubernetes, existing Prometheus; stdlib Python request probe on the load generator. No dependencies.

**Spec:** ../specs/2026-10-31-idle-reactivation-design.md; author amendment: a new workflow, not a loadtest profile.

## Global Constraints

- One function from the five B5 runtimes; two treatments, identical immutable images and resources.
- CP 2 CPU/2048 MiB; function 2 CPU/512 MiB; fixed concurrency 2, queue 20, server retries 3, client retries 0.
- INTERNAL min/max 1/1 versus 0/1; normal polling/cooldowns. Cached-image reactivation only.
- Pilot three cycles, 180-second idle intervals, last 60 seconds qualified; warmup 60 requests at one-second minimum spacing, five early subsequent requests.
- Workflow-owned Spot provisioning; isolated checkouts and all campaign outputs under /data; existing running experiments untouched.
- Raw request and lifecycle evidence survives failures. Missing observations never mean zero.

## Review Focus

- Terminating old pods and missing Kubernetes replies cannot qualify zero; task 2.
- Client timeout does not prove server cancellation; stop unresolved arm without another idle cycle, task 2.
- Same pod or changed image cannot qualify reactivation; task 2/3.
- Gauge labels and stale terminated-pod series must not become live memory; task 3.
- Partial CLI/resume and acquisition failures cannot create an apparently complete paired campaign; task 1/3.

### Task 1: Dedicated scenario and integration

Files: config/idle_reactivation.py, config/scenario.py, application/provisioning.py,
plans/dispatch.py, cli/product.py; tests/config/test_idle_reactivation.py and plan/CLI tests.

Interface: `IdleReactivationConfig(cycles=3, idleSeconds=180, zeroFirst=False)`;
workflow consumes only its block, backend/build/functions/resources.

- [x] Write tests that dedicated config parses and rejects loadtest knobs, unsupported functions/budgets and missing protocol.
- [x] Run tests RED; implement strict schema and integrate workflow provisioning/URL forwarding/unique run directory.
- [x] Check plan/CLI registration, reject partial/resumed runs before acquisition; run focused tests GREEN.

### Task 2: Measurement and immutable evidence

Files: tasks/idle_reactivation.py, assets/diagnostics/idle_reactivation_probe.py;
tests/tasks/test_idle_reactivation.py, tests/tasks/test_idle_reactivation_probe.py.

Interfaces: remote probe emits flushed JSONL request records with monotonic full-body duration;
`RunIdleReactivation` Sonata task composes lifecycle observations, requests, fetching and snapshot;
`qualifies_idle` and offline paired analysis consume raw records, never fake observations.

- [x] Test a real local HTTP server for exact oracle qualification, failures, redirects and sequential request ledger; observe RED.
- [x] Implement stdlib remote probe using B5's generated contract/corpus, explicit no retries, bounded deadlines.
- [x] Test zero/readiness/UID/image predicates, missing samples, timeout abort and artifact recovery; observe RED then implement.
- [x] Reuse raw Prometheus series via opt-in label retention, keeping default loadtest aggregation unchanged; test both.

### Task 3: Workflow composition and paired reporting

Files: plans/idle_reactivation.py, paired validation in tasks/idle_reactivation_report.py,
five assets/presets/scenarios/idle-reactivation-*.yaml, docs/b6-idle-reactivation.md.

Interface: `build_idle_reactivation_plan(config, environment, bindings, *, control_plane_url,
prometheus_client, run_dir, repo_root, tool_root, fetcher)` returns a complete Sonata Workflow.

- [x] Write recording-executor integration tests showing build-once, same images, min/max values, release-before-next-acquire and cleanup on failure; observe RED.
- [x] Build from `resolve_function`/`sonata_function`, `PlatformRequest`, `control_plane_helm_values`, `add_platform`; do not import the loadtest plan.
- [x] Add paired identity/accounting gate; preserve both arms and failure summaries before failing the workflow.
- [x] Add bundled presets and documented plan/run/reanalysis commands; verify distribution inclusion.
- [x] Run focused tests, complete package suite, lint/type checks relevant to changes, CLI plan for five presets, and fresh independent review.

## Execution ledger

- Ruling: use completion-relative equal idle exposure, not an open-loop arrival calendar, as clarified in the design review. Whole-cycle durations differ and must remain explicit.
- Ruling: if a timed-out remote request leaves server completion unobservable, stop that arm rather than invent cancellation or a drain proof. The other arm can still retain useful diagnostic evidence; the paired gate remains failed.
- Ruling: author authorized the dedicated workflow and implementation; proceed inline without another plan approval round.


## Verification and review evidence

- 78 targeted tests passed, covering strict config/CLI, real HTTP probe, numeric
  cross-runtime equality, labelled gauges, lifecycle and memory boundaries,
  offline interrupted records, provisioning and architectural import contracts.
- Recording-executor integration passed both successful and failed measurement
  paths: one image build, two matched registrations, both Helm uninstalls before
  the final evidence gate, and retained summaries on failure.
- All five bundled presets produced CLI plans against a managed Azure Spot
  environment. Offline wheel build succeeded and contained all eleven workflow,
  probe and preset files. No dependencies were added.
- Ruff passed across NanoLab source/tests; changed files passed basedpyright.
- Independent review found and resolved numeric JSON equivalence, Azure implicit
  user paths, incomplete-arm reporting, and offline lifecycle revalidation.
  Burst observations now reject intermediate pod replacement.
- Full package run on macOS: 5755 passed, 14 skipped, 398 failed and 45 setup
  errors (603.94 seconds). Five B6 imports saw a stale pre-edit module during
  this overlapping run; all B6 tests pass in the final fresh 78-test run above.
  All 438 existing failing/error nodes reproduce on clean base `0c17552`:
  393 failures and 45 setup errors, identical node sets (117.65 seconds).
  For example, Sonata owned-process tests require Linux pidfds and procfs.
  The full suite is not green on this host.
- Whole-package type checking also reports nine errors in pre-existing B1/loadtest
  tests, reproduced unchanged on clean base. All changed source and tests pass.
- No cloud run, Kubernetes hardware qualification, final campaign analysis,
  merge or publication has occurred for this workflow.
