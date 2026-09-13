# P24 single-version soak: implementation ledger

## Workspace and scope

- Worktree: `/tmp/nanolab-p24-soak`.
- Branch: `codex/p24-single-version-soak`, based on `f5fcf3c`.
- Original NanoLab checkout and NanoFaaS implementation left unchanged.
- Original image-override/push changes imported without conflicts; no commit or merge back to main performed.
- Reference: `2026-09-13-p24-single-version-soak.md` and its approved build-from-source amendment.

## Implemented checkpoint: tasks 1 and 2

- [x] Dedicated strict `SoakConfig`, separate from comparison/loadtest configuration.
- [x] Build-by-default role image specifications; explicit prebuilt mode requires immutable image reference and provenance receipt.
- [x] Per-role limits, required metrics, collection sources, runtime options, workload rates, prerequisites and diagnostic budgets.
- [x] P24 retention-derived minimum load/drain duration and baseline-drain validation; shorter smoke remains explicitly separate.
- [x] Criteria require declared metrics, units, thresholds/tolerances, deadlines and rationale; every process requires memory budget and RSS residual policy.
- [x] Cross-check selected functions, workload rates, image roles and optional top-level resource limits; reject ignored legacy knobs.
- [x] Immutable target/sample/result values and clock/probe/diagnostic/workload ports.
- [x] Incremental JSONL evidence storage with exclusive run-directory ownership, bounded record size and quota reserving terminal-report space.
- [x] Atomic immutable JSON documents, canonical fingerprints and streaming artifact hashes.
- [x] Preserve valid record prefixes, expose torn tails as observation gaps and reject corrupt complete records.
- [x] Targeted RED/GREEN cycles and regression tests.

Production additions: `config/soak.py` and `tasks/soak/{__init__,models,ports,artifacts}.py` under `packages/nanolab/src/nanolab/`. Integration change: `config/scenario.py`. Tests: `tests/config/test_soak.py` and `tests/soak/test_artifacts.py` under `packages/nanolab/`.

## Evidence

Commands used the existing NanoLab virtualenv with `PYTHONPATH` pointing at the worktree and `NANOFAAS_ROOT=/home/michele/Documenti/nanofaas`; dependencies were not reinstalled.

- Task 1 RED: 37 failed, 40 passed. Valid new scenarios and direct new-module tests failed before implementation; old-schema rejection already satisfied some negative cases. `/tmp/nanolab-p24-task1-red.log`.
- Task 1 GREEN plus existing scenario tests: 101 passed. `/tmp/nanolab-p24-task1-green.log`.
- Task 2 RED: 22 failed before the artifact implementation. `/tmp/nanolab-p24-task2-red.log`.
- Task 2 GREEN plus scenario tests: 123 passed. `/tmp/nanolab-p24-task2-green.log`.
- After formatting, combined new tests and existing scenario/loadtest/resource/comparison regressions: **177 passed**. `/tmp/nanolab-p24-foundation-tests.log`.

No full-package suite, static gate, Docker build, runtime smoke or long soak was run at this checkpoint. GitNexus reported MEDIUM impact on ScenarioConfig (28 affected symbols, 11 direct); its index predates these additions and is not a post-change completeness claim.

## Open integration regression

`tests/plans/test_soak_prebuilt_images.py` was introduced while integrating the original changes. Its three platform-request cases passed; `test_native_soak_preset_compiles_without_building_images` failed because the legacy native preset enters comparison routing without a control-plane image. That case remains open and was not included in the 177-test command above. The subsequent user requirement is build-from-source by default, so the resolution belongs to the new build-aware workflow, not a workaround forcing prebuilt tags. Do not report all tests green or the native preset usable.

## Next work and remaining acceptance

Task 3 is not implemented. The existing `images/plan.py` catalogue supplies native/JVM/default cells and build prerequisites; `images/control_plane_variants.py` supplies variant tuning. Use these instead of maintaining a second recipe catalogue. Existing `release/build.py:create_source_archive` intentionally requires a clean commit: preserve that release guarantee and add a soak-specific stable snapshot path that includes identified local modifications.

Tasks 3-12 remain: build/snapshot receipts and effective preflight; process probes and continuous observer; verified diagnostics and attribution; offline evaluator/report; cancellable workload driver; prerequisite receipts; Sonata lifetime and cleanup; CLI/TUI routing; runnable presets/docs; integration smoke and final gate.

The schema now accepts `workflow: soak`, but its execution route is not yet installed. **The new workflow is not executable or ready for P24.** No successful build, smoke, soak or complete merge is claimed. Continue implementation from task 3; approvals already obtained cover isolated work and targeted RED/GREEN tests, not an automatic expensive image build or long run.

## Task 3 implementation checkpoint: incomplete

Added `tasks/soak/sources.py`, `images.py` and `preflight.py` with corresponding source-snapshot, image-recipe and preflight tests.

Source handling captures tracked, modified and nonignored untracked files, records deleted inputs and executable modes, rejects unsafe symlinks and source changes during capture, and materializes independent build workspaces. The release clean-source requirement is unchanged.

Image preparation reuses the existing image catalogue and Bake renderer, preserves per-role JVM/native/default flavor and module selection, creates run-specific tags and validates output/provenance receipts. No Docker build was executed. The actual execution of these recipes remains part of the workflow integration, not a completed live build path.

Preflight validates effective CPU/memory limits including cpuset bounds, source/image identity, runtime options, modules, required observations, retention, disk space and generator capacity; it persists a preflight-only report.

RED evidence: `/tmp/nanolab-p24-task3-red.log` (39 failures before implementation) and `/tmp/nanolab-p24-task3-preflight-red.log` (34 preflight failures, including subsequently added cases).

First implementation verification: **46 passed, 8 failed**, `/tmp/nanolab-p24-task3-green.log`. All eight image test failures reach the same adapter error: `plan_images` passes `0.0.0-soak` to `build_image_plan`, but `normalize_version` rejects that format. This is an implementation defect, not an environmental issue. It has been reported to the user and has not yet been corrected. Task 3 is not complete; no post-change full/static gate or real image build was performed.

### Task 3 correction checkpoint (2026-09-13)

- User approved correcting the image catalogue version-format failure.
- GitNexus upstream impact for `plan_images` returned UNKNOWN with no resolved callers/processes. Text search confirmed references in the implementation and new image tests only; UNKNOWN was not treated as a graph all-clear.
- Changed the temporary catalogue version from `0.0.0-soak` to valid `0.0.0`. Catalogue tags are replaced with run-specific tags before building; JVM/native selection is unchanged.
- Formatted the six new task 3 implementation/test files.
- Focused task 3 tests: 54 passed. Log: `/tmp/nanolab-p24-task3-correction.log`.
- Selected task 1-3 and existing scenario/loadtest/runtime-comparison regressions: 231 passed. Log: `/tmp/nanolab-p24-task3-regressions.log`.
- Source snapshot, image recipe/receipt and effective-preflight primitives are implemented. Actual build execution and complete workflow orchestration are not yet wired; tasks 4-12 remain outstanding.
- The previously recorded native legacy preset compilation failure is excluded from this selected suite and remains unresolved. This is not a full-suite success claim.
- No application image builds, soak runs, commits or merges performed at this checkpoint.

### Task 4 first implementation checkpoint (2026-09-13, partial)

- Added bounded Prometheus text parsing preserving labels and escaped values, accepting optional timestamps and rejecting malformed, duplicate or nonfinite series. No parser dependency added.
- Added distinct procfs RSS/PSS parsing in bytes. Missing/inaccessible fields remain unavailable rather than zero.
- Added absolute-clock scheduling and a continuous observer with immediate JSONL persistence, phase transitions, cancellation-aware waits, bounded per-scrape counts, missed-deadline events and surfaced worker/write failures. No historical label union or sample buffer is retained by the observer.
- Existing ProcessProbe protocol and legacy loadtest observer were not modified. GitNexus impact for ProcessProbe returned UNKNOWN, not a low-risk assurance.
- RED: 24 tests failed for missing new modules. GREEN: 24 passed, including 100000 synthetic changing-label samples, slow scrapes, disk quota failure and drain cancellation.
- Selected accumulated regressions: 255 passed. Logs: `/tmp/nanolab-p24-task4-red.log`, `/tmp/nanolab-p24-task4-green.log`, `/tmp/nanolab-p24-task4-regressions.log`.
- Task 4 is NOT complete: role-bound Docker/HTTP transports, raw cgroup statistics, restart/identity change detection, required-metric gap conversion and per-probe I/O timeout tests remain. The observer requires bounded-I/O probes; its join timeout cannot terminate arbitrary blocked Python I/O. No live collection readiness is claimed.
- Previously recorded native legacy preset failure remains excluded and unresolved. No full suite, real build, soak, commit or merge performed.

### Task 4 role-bound adapters checkpoint (2026-09-13, still open)

- Added `tasks/soak/adapters.py` and `tasks/soak/collector.py` with separate adapter/collector tests. No existing production symbol or legacy loadtest module was edited. GitNexus query/context located existing collection flows; ProcessProbe graph coverage remains unresolved and its contract was preserved.
- RoleBoundProbe binds control-plane/SDK/proxy targets independently; validates container ID, PID, StartedAt, running state and image RepoDigests before and after collection. Identity mismatch invalidates the scrape, and required missing values become unavailable samples with reasons.
- Preserved procfs RSS/PSS, raw Docker memory usage/limit and labeled raw memory-stat fields as distinct measurements. Derived working set is explicitly named an estimate. Exposition cannot overwrite process/cgroup sources, and series count never substitutes for registry meter population.
- Collector uses read-only Docker Engine API over an explicit Unix socket, bounded procfs reads and bounded HTTP(S) exposition. It requires local Linux procfs ownership and container-init PID correspondence; unsupported/remote procfs yields missing evidence. Redirects and environment HTTP proxies are disabled.
- Each collection runs in a dedicated Python child with an overall subprocess timeout. The helper spawns no descendants and bounds response/output sizes. Unit tests cover timeout forwarding and conversion; no real hanging Docker/HTTP process test was run, so live timeout behavior is not an integration-tested claim.
- RED: 27 missing-module failures. GREEN: 27 adapter/collector tests passed. Selected accumulated regressions: 282 passed.
- Logs: `/tmp/nanolab-p24-task4-adapters-red.log`, `/tmp/nanolab-p24-task4-adapters-green.log`, `/tmp/nanolab-p24-task4-adapters-regressions.log`.
- Discovered issue in previously written Observer.start: hard-coded five-second first-sample wait can reject a valid multi-role collection whose configured budget exceeds five seconds. Reported to the user; not silently corrected. Task 4 remains open pending approval to fix this with a regression test and finish lifetime validation.
- No Docker endpoint contacted, image build, soak, full-suite/static checks, commit or merge performed. Known legacy native preset failure remains outside the selected regression suite and unresolved.

### Task 4 authorized startup-timeout correction (2026-09-13)

- User approved correcting the hard-coded first-sample startup timeout.
- Observer now accepts a validated keyword-only `startup_timeout_s` and uses it for the readiness wait. The default remains five seconds for compatibility; task 9 wiring MUST pass a budget covering the complete sequential first sweep across all roles, including overhead. This change does not claim automatic budget derivation or completed workflow integration.
- GitNexus index refresh exited 1 without a captured explicit error; it also reported truncated flow coverage. Subsequent upstream impact found Observer but returned UNKNOWN with no resolved callers/processes. Text search found callers only in soak tests. No clean graph assurance is claimed.
- Added seven regression tests: configurable budget above five seconds (simulated wait, not a real six-second collection), invalid budgets rejected, and startup expiry followed by cancellation/join with a releasable probe.
- RED: seven failures for the missing parameter. GREEN: 43 targeted observer/adapter/collector tests passed. Accumulated selected regressions: 289 passed.
- Logs: `/tmp/nanolab-p24-task4-startup-red.log`, `/tmp/nanolab-p24-task4-startup-green.log`, `/tmp/nanolab-p24-task4-startup-regressions.log`. Index log: `/tmp/nanolab-p24-task4-startup-index.log`.
- The reported hard-coded timeout issue is addressed at the observer API. Real Docker/HTTP timeout and end-to-end lifetime validation remain outstanding; this checkpoint does not mark the complete workflow ready.
- No Docker access, image builds, soak runs, full-suite/static checks, commits or merges. Previously recorded legacy native preset failure remains excluded and unresolved.

### Task 5 diagnostic adapters and evidence checkpoint (2026-09-13, integration pending)

- Added `tasks/soak/diagnostics.py` and `tests/soak/test_diagnostics.py`; no existing production symbols or legacy diagnostic/loadtest code were changed. GitNexus concept query located related runtime/receipt flows before implementation.
- Added JVM, Node and explicitly configured native adapter logic behind a DiagnosticExecutor port. Capability evidence, helper digest, runtime compatibility, attachment access and bounded execution are required inputs; Node additionally requires private control. No helper is provisioned or inspector exposed automatically.
- JVM requests use explicit jcmd argv, including GC.run, class histogram, heap dump and an explicitly named existing JFR recording. Node requests use structured inspector protocol methods. Native advertises only configured and observed operations; JVM-specific native metrics remain not_applicable while required missing evidence is unavailable.
- Captures verify target identity before/after execution, require a completed and hash-verified natural final checkpoint, reserve shared count/byte budgets before execution and check available disk. Reservations are conservative and are not refunded after failed attempts.
- Receipts preserve command/helper identity, timing, exit/completion status, output hashes and perturbation windows. Counter increases alone do not verify a full GC: a matching full-cycle event must identify the target, request, configured source and interval. Timeout/failure leaves the perturbation end unknown rather than asserting the target operation stopped.
- Added attribution validation for owner/population/lifetime/budget/reviewer and artifact integrity. Its PASS is attribution-only and cannot replace numerical checks. The task 6 evaluator must independently match the referenced policy to the frozen run policy; this validation alone does not authorize any new budget.
- RED: 32 missing-module failures. GREEN: 32 diagnostic tests passed. Accumulated selected regressions: 321 passed.
- Logs: `/tmp/nanolab-p24-task5-red.log`, `/tmp/nanolab-p24-task5-green.log`, `/tmp/nanolab-p24-task5-regressions.log`.
- Official command/API references consulted: https://docs.oracle.com/en/java/javase/25/docs/specs/man/jcmd.html and https://nodejs.org/api/inspector.html .
- IMPORTANT: tests use a simulated executor. A provisioned real DiagnosticExecutor, compatible helper images, namespace/path mappings, private Node transport and enforcement of execution/output limits remain integration work. The port contract and post-capture validation are not proof of live enforcement. Task 5 is not declared operationally complete.
- Task 4 real Docker/HTTP timeout/lifetime validation and task 9 startup-budget wiring remain open. Known native legacy preset failure remains excluded and unresolved. No real diagnostic, Docker access, build, soak, full-suite/static check, commit or merge performed.

### Task 6 numerical evaluator/report checkpoint (2026-09-13, partial)

- Added `tasks/soak/evaluate.py`, `tasks/soak/report.py` and their focused tests. Existing production symbols were not modified; GitNexus concept query was used before implementation.
- Added outcome precedence ABORTED > FAIL > INCONCLUSIVE > PASS while retaining individual criterion outcomes. Numerical violations are not hidden by incomplete final sampling where a valid baseline/reference exists.
- Added disk-backed SQLite indexing of saved samples and label identities with bounded SQLite cache and file-backed temporary sorting. The evaluator preserves independent label series rather than summing them, checks process identities/timing/units/availability, and records corrupt or torn streams as incomplete evidence.
- Added maximum, expected-zero, return-to-reference and growth-review numerical checks. Recovery compares the natural reference median with the maximum in the declared window, using the stricter absolute/relative tolerance. Declared deadline, when present, ends that evaluation window. Growth above the review threshold requests equal-work evidence/attribution rather than automatically labeling a leak.
- Diagnostic-phase samples and declared perturbation overlaps cannot substitute for natural recovery evidence. Required absent SDK/metric/label series remain INCONCLUSIVE.
- Added immutable JSON and Markdown per-criterion/role reports in new `evaluations/evaluation-<id>` directories. Reports preserve FAIL details under ABORTED and explicitly label scope numerical-only and p24_qualified=false.
- Introduced `evaluation-input.json` as an explicitly derived numerical projection (criteria, targets, natural windows, sampling/gap policy and perturbations). This is NOT a replacement for the frozen SoakConfig policy. Task 9 must derive it from that policy; independent binding verification is still required.
- RED: 27 missing-module failures. GREEN: 27 evaluator/report tests passed, including offline determinism with network/subprocess access forbidden. Accumulated selected regressions: 348 passed.
- Logs: `/tmp/nanolab-p24-task6-red.log`, `/tmp/nanolab-p24-task6-green.log`, `/tmp/nanolab-p24-task6-regressions.log`.
- Task 6 is NOT complete: full frozen-policy/effective-preflight binding, workload conservation/correctness, prerequisite coverage, diagnostic completion, equal-work attribution and complete run acceptance gates remain unimplemented. evaluate_run deliberately always includes run-coverage INCONCLUSIVE. Supplied attribution cannot waive numerical checks and remains unbound at this checkpoint.
- Tasks 4/5 live transport/helper validation and task 9 wiring remain open. Known native legacy preset failure remains excluded and unresolved. No live runtime access, build, soak, full-suite/static checks, commit or merge performed.

### Continuous plan completion requested (2026-09-13)

- User requested continuing through completion, rather than stopping after each numerical checkpoint. Independent tasks delegated with disjoint write scopes; no commits, real Docker/build/diagnostic/soak operations authorized by this dispatch.
- Main owns CLI/TUI integration; Maxwell owns workload/task 7; Gauss prerequisites/task 8; Epicurus lifecycle/build composition/task 9; Cicero full acceptance/task 6; Descartes presets/docs/task 11. Huygens reviews task 7/8 read-only.
- Task 7 reported 16 focused tests passing including synthetic child/grandchild reaping. Task 8 reported 20 focused plus 32 existing validation tests passing; its isolated production profile runner remains integration work.
- CLI/TUI patch: explicit soak routing, unique run directories, resume/selection guards, terminal-aware metadata/exit codes, offline soak-evaluate registration and explicit external policy loading. Focused new CLI tests RED 18 failures then new/existing CLI/TUI GREEN 44 passed. Logs: `/tmp/nanolab-p24-cli-red.log`, `/tmp/nanolab-p24-cli-green.log`.
- Pre-edit graph risk was HIGH for cli.product._workflow (11 impacted symbols, run/plan, CLI/TUI/comparison areas) and CRITICAL for _scenario (10 impacted symbols, run/plan/inspect/comparison/main/TUI). Warned before patching; changes gated to workflow soak. Metadata/default-directory methods LOW. Nested Typer run/plan UNKNOWN; their actual command registration confirmed textually.
- Ruling: external policy uses top-level soakPolicyFile and a nanolab-soak-policy-v1 document containing criteria only; it cannot override runtime/images/timing. Reason: require explicit pre-run numerical policy without silently altering the measured environment. Cost if wrong: future policy needs require an explicit schema extension, not a permissive overlay.
- Resolved policy input is attached privately to ScenarioConfig as _soak_policy_receipt for workflow persistence; the resolved SoakConfig still fingerprints the actual criteria.
- Task 9 initial delivery is partial: production builder/teardown remain unavailable and two tests use invalid RoleBindings fixtures. Controller requested user approval for correcting fixtures and subsequently discovered defects, while continuing unaffected integration. Task 9 was directed to make planning compile real stages without inventing successful runtime evidence.
- This checkpoint is not plan completion. Full acceptance, production adapters/lifecycle, presets and final integration/static/regression gates remain active. Existing native legacy preset failure remains unresolved until new routing/preset integration passes.

### Task 7/8 review findings and pending fix approval

- Read-only reviewer Huygens found: P1 detached setsid grandchildren can escape workload process-group cleanup; P1 prerequisite async context teardown can outlive the body timeout and block terminal persistence; P1 k6 stdout/log and generation output bypass the artifact quota. P2 counter conservation/aggregate consistency is not checked before completed=true; P2 producer schemas diverge from the shared schema.
- Findings were surfaced to the user and implementers. Corrections are pending the requested authorization to fix the faulty task 9 fixtures and subsequent defects without stopping at each fix. No implicit approval inferred from subagent notifications.
- Task 11 delivered four single-version build-default presets and operator docs. Agent reported 99 preset/config tests passing. P24 requires an explicit criteria policy file; the short prerequisite preset is explicitly smoke and does not satisfy P24 prerequisite acceptance.
- Integration mismatch to resolve: shipped smoke uses distinct preallocated/max VUs while the current workload factory rejects them. Real runner/gate wiring must honor both rather than silently replacing policy.
- No claim of completed plan or readiness for an expensive smoke/soak is justified at this checkpoint.

### Combined checkpoint and explicit remaining blockers

- Selected combined regression command (all tests/soak, soak/scenario configuration, new CLI and existing TUI, existing loadtest/resources/runtime-comparison): 497 passed in 3.82s. Log: `/tmp/nanolab-p24-completion-checkpoint.log`.
- This command excludes tests/plans/test_soak.py with the two previously reported invalid RoleBindings fixtures, and excludes the historical tests/plans/test_soak_prebuilt_images.py integration regression. It is not the full package/static gate.
- Acceptance agent delivered acceptance.py and changes to evaluate.py/report.py plus 27 additional acceptance tests; its focused evaluator/report/acceptance set had 54 passing tests. Manifest contract is acceptance-manifest.json binding config/preflight/workload/prerequisite/diagnostic/artifact references and phase timings.
- Acceptance remains unsafe as a final readiness gate: it expects normalized receipt schemas not emitted by tasks 7/8, does not independently prove admission sources, trusts normalized prerequisite statuses instead of invoking validate_receipt, lacks aggregate/per-function reconciliation and does not cover the discovered lifecycle/quota defects. These are explicitly reported blockers, not waived by synthetic PASS fixtures. Do not use a positive synthetic report to claim P24 readiness.
- Production build_soak_plan and teardown_soak_run still raise SoakIntegrationUnavailable at construction. The proposed runtime.py/staged production builder was not applied. compose_frozen_soak_workflow, build task primitives, terminal persistence and lifecycle bridges exist, but do not make the public workflow executable.
- Remaining work by area: task 3 observed build provenance/runtime bindings; task 4 real transport/lifetime validation; task 5 real bounded helper executors; task 6 genuine producer-receipt/policy acceptance; task 7/8 reviewed cleanup/quota/schema fixes and isolated profile runner; task 9 production builder/retained teardown/setup failure evidence; task 10 real builder/keep/teardown validation; task 11 readiness claims/policy inputs; task 12 actual integrated fake end-to-end, full static/regression gates, then separately authorized build/smoke/P24.
- User was asked to authorize correction of the two fixtures and the further defects found during final checks without requiring a separate approval for every fix. No such reply has been received at this checkpoint. The plan is NOT complete; no commit/merge, live build, diagnostic, smoke or soak has been executed.

## 2026-09-13 - Authorized correction and integration checkpoint

Status: the authorized correction batch is complete and regression-tested.
The full P24 implementation plan is NOT complete, and the public soak workflow
is NOT yet ready for a real run. No task is declared operationally complete on
the strength of synthetic tests alone.

### Corrections landed

- Fixed `BuildImagesTask` to resolve its declared snapshot through Sonata's real `TaskInputs.resource` API. Added an execution-path regression, not just a constructor test.
- Separated the API endpoint from the Compose management/readiness URL in frozen soak composition. Fixed invalid executor-binding fixtures.
- Preserved fractional workload rates and separate preallocated/max VU values in the lifecycle bridge. The workload driver now allocates both budgets globally across functions instead of multiplying them by the number of scenarios.
- Added independent scheduled-demand conservation checks: per-function offered plus dropped iterations must match rate times duration within at most one boundary iteration. Existing aggregate/per-function result conservation remains required.
- Added descendant-aware command supervision, bounded log/summary output, minimum supported stop budgets, and a fresh final reaping window. Diagnostic execution now uses the same local supervisor rather than trusting a helper-reported cleanup flag.
- Gave prerequisite acquisition, exercise, and release independent deadlines inside an owned worker. Partial evidence survives stubborn cleanup; unconfirmed release prevents PASS. Cancellation and background-task lifetime regressions remain covered.
- Updated offline acceptance to consume native prerequisite/workload receipts and replay prerequisite validation. Source/build/recipe/script/payload binding, authoritative admission samples, global VU sums, and scheduled demand are independently checked.
- Added a bounded host-only `OwnedBuildCommandExecutor` and `BuildProvenanceCollector`. Fixed their integration allowlist to accept digest-pinned published `.Provenance`, alongside Manifest and Image inspection.
- Fixed soak-specific `keep` behavior: Compose journal records now contain JSON-safe project identity and cwd; replay rejects another project's record. Only soak function resources opt into retention. Shared function/Compose factory defaults are unchanged.
- Strengthened real-Sonata tests for successful/failed measurements with and without keep, no premature release before journal replay, reconstruction with fresh resource objects, identity mismatch rejection, and repeated teardown replay.

### Review and graph scope

- Review found and drove fixes for detached diagnostic descendants, the final cleanup window, multiplied VU limits, underdelivered scheduled work, and insufficient keep assertions.
- The stronger keep test exposed an actual serialization failure: Sonata could not journal the Compose dataclass's Path field. It also exposed the shared function factory's intentional always-release default.
- `function_resource` impact was HIGH (12 upstream impacts); it was not modified. Retention behavior was adapted only in the soak composition.
- New soak symbols remained UNKNOWN or absent in the partial/stale graph. This was not treated as proof of safety. Direct contracts, known callers, and targeted regression tests were used as additional evidence.
- No commit, merge, graph reindex, image build, registry publication, Docker deployment, live diagnostics, smoke, or P24 soak was performed in this batch. The NanoFaaS checkout was not edited.

### Final validation

- Combined selected regression suite: **680 passed in 20.79s** after all correction and style changes.
- Scope: all `tests/soak`, soak/scenario configuration, soak CLI, TUI workflow/controller, soak composition, loadtest/resources, runtime comparison, and migrated function-resource tests.
- Ruff: **all checks passed** for the correction batch's selected implementation and test files, including prerequisites, workload/processes, diagnostic executor, build executor/provenance, acceptance/evaluate/report, lifecycle, and retention.
- Combined test log: `/tmp/nanolab-p24-completion-regression.log`.
- Ruff log: `/tmp/nanolab-p24-completion-ruff.log`.
- The earlier 620-pass/2-failure checkpoint is superseded: both keep failures are fixed. The old `test_soak_prebuilt_images.py` native-preset compilation test remains outside this selected suite and needs migration together with the actual public builder. This is not a full-package validation claim.

### Remaining work before plan completion

1. Implement `build_soak_plan`: connect deferred source preparation, actual build-stage toolchain observation capture, provenance collection, frozen digests, run-owned Compose/endpoint allocation, and deployment. The collector currently requires a real request-bound observations sidecar; having its validator and bounded transport does not produce that evidence automatically.
2. Connect deployed process discovery and effective preflight observations to the continuous observer. Provide the live prerequisite runner and the explicitly provisioned diagnostic helper/protocol where required. The helper executor is not a provisioner and its receipt is an operator trust boundary, not an independent attestation.
3. Emit the complete acceptance manifest and all native producer references from the actual run, including source/recipe/build records, workload inputs/script, prerequisite inputs, timing, targets, diagnostics, and artifact inventory. Synthetic acceptance fixtures are not a substitute.
4. Implement `teardown_soak_run` from persisted run ownership and the Sonata journal. The lower-level journal-safe adapters are tested; the public teardown entry point still explicitly refuses execution.
5. Finish the remaining public-builder/preset integration regressions and end-to-end review. Only then request explicit image-build and real smoke/soak budgets and run the authorized operational gates.

Ruling unchanged: `soakPolicyFile` overrides acceptance criteria only, not runtime,
images, or timing. Expanding that policy boundary requires an explicit schema
change; it must not silently change the environment being measured.

## Continuation: real-run integration pending

- Active objective remains completion of the workflow followed by an actual P24 soak; no completion claim.
- Host observed ARM64 with Docker 29.2.1, BuildKit 0.27.1, Java 25.0.4 and k6 2.2.0. Registry image pull completed; no application deployment or soak started at this checkpoint.
- Explicit ARM64 runtime scenario copies are in `/tmp/nanolab-p24-inputs/`; repository platform defaults were not silently changed through the criteria-only policy.
- Build-stage observation implementation reports 61 synthetic tests passing; live build evidence remains outstanding.
- New teardown/ownership tests passed in focused runs, but review found ownership-on-registration-failure, historical retention-title reuse, cleanup dependency preservation, effective Compose identity and control-plane continuity risks. Corrections are still in progress; those earlier passes do not close these findings.
- Live prerequisite adapter reports 20 synthetic tests passing. It explicitly reports missing authoritative retained-population metrics; full P24 readiness is not established and TTLs have not been shortened.
- Integration of the public deferred builder, real diagnostic helper and corrected cleanup ownership remains pending. Smoke and full P24 are distinct gates, and no synthetic receipt or shortened run may substitute for the full experiment.

## Real validation checkpoint: source and diagnostic provisioning

- Main reran selected build/cleanup/helper tests: 120 passed in 4.36s. Main reran prerequisite adapter/validator tests: 79 passed in 2.47s. Source snapshot correction: 18 passed in 0.27s. These are focused suites, not a whole-package pass or completed P24.
- Independent static review found no additional P1/P2 in the seven corrected cleanup files. Arbitrary acquisition-crash recovery remains outside that claim.
- Helper ARM64 image built successfully from digest-pinned bases. Build/toolchain/kernel-probe artifacts: `/tmp/nanolab-p24-helper-build.sG6L1c`. Actual container checks observed Python 3.13.15 and Temurin/jcmd/JFR 25.0.4.
- Published helper reference: `localhost:5000/nanolab/p24-diagnostic-helper@sha256:b617e4ced631bc03de242f1cbba015398771378513cf5fc5eaa5fc67b09b3ef9`.
- A real same-UID 65532, cap-drop-ALL shared-target-PID probe read target smaps_rollup successfully. This proves that particular kernel-access setup, not Java/Node provisioner readiness or universal PSS availability. Temporary probe containers were removed.
- Owned validation registry is still running on 127.0.0.1:5000. Container ID: `386c381d8d96eb836f51dac7bc20eaa4fe271a0cd66df5c8cc39dd0df874fda1`; identity/data directory: `/tmp/nanolab-p24-registry.dvmV2X`. This externally prepared dependency is not yet automatically provisioned by the public workflow.
- First real product CLI smoke exited 2 / INCONCLUSIVE before application builds: source symlink target not captured, `scripts/ansible`. CLI log: `/tmp/nanolab-p24-inputs/smoke-cli.log`.
- Diagnosis established an existing internal dangling link, `scripts/ansible -> ../ops/ansible`, with missing `ops`. The source snapshot now preserves safe internal dangling symlinks faithfully, while still rejecting escapes, cycles, access failures and existing uncaptured targets. NanoFaaS and its link were not changed. A CLI retry is still required.
- Standalone real JVM helper preparation failed before attachment probes because Docker rejected the bind source `/proc/<host-pid>/root/tmp` with invalid argument. Evidence: `/tmp/nanolab-p24-inputs/standalone-jvm-b101d7ae75bd47a988c64d802e9e9520/standalone-validation.json`. Cleanup reported no errors. This was not a GC completion or P24 success.
- Corrective diagnostic topology agreed for implementation: an explicitly created, owned Docker local tmpfs volume shared at target/helper `/tmp`, with fixed finite options and checked ownership. The provisioner must not create missing volumes implicitly. Separate helper `/out` quota remains required. Implementation and live retest are pending; memory-only helper needs no shared mount.
- Public runtime memory-helper integration is in progress. No application image set has yet been built/frozen by a successful product run, and no full workload soak has started.
- User decision requested asynchronously on adding necessary NanoFaaS observability versus leaving the platform unchanged and reporting P24 non-qualifiable. Missing instrumentation must not become a fabricated zero or an unjustified non-applicability exemption. RSS criteria and configured durations remain unchanged.

## Real validation checkpoint: successful source capture, build-driver blocker

- Previous continuation made concrete progress; the goal remains active, not complete.
- Real whole-checkout source capture and verify succeeded. Snapshot: `/tmp/nanolab-p24-inputs/source-validation-e9110659c3244bdcb03efa291b5bd342`; fingerprint: `924bc456e249a9550ee4d12ed01526b2dc66f2d8317f4498e98793bb24bc23db`.
- Actual Docker test proved an owned local tmpfs volume can be shared by same-UID, cap-drop-ALL containers, with observed capacity exactly 33554432 bytes. All probe containers and that volume were removed. Log: `/tmp/nanolab-p24-helper-build.sG6L1c/shared-tmpfs-probe.log`.
- Runtime memory-only helper wiring was integrated using frozen `soak.diagnostics.helper_images` role keys. Only `/tmp/nanolab-p24-inputs/smoke-arm64.yaml` was updated with the three helper references; operations remain empty. Main ran plan/runtime/preparation tests: 30 passed in 0.16s.
- Second product CLI attempt used `/tmp/nanolab-p24-smoke-arm64-20260913-a`. Source capture passed in 2.4s; real control-plane Gradle prerequisite compilation passed in 11.1s. Application image build then failed because the current default Docker driver does not support attestations. CLI exit 2 / INCONCLUSIVE; no deployment or workload started. CLI log: `/tmp/nanolab-p24-inputs/smoke-cli-a.log`; failure command log: `build-command-logs/command-b66e2cfef7a944f8b017dd9331b9963c.log` under that run.
- Attestations were not removed or replaced with synthetic provenance. User approval was requested for a dedicated resource-limited BuildKit builder: Docker's docker-container driver runs it privileged. No such builder was created or started, and the Docker daemon was not changed.
- Resolved only the prospective BuildKit ARM64 image manifest: `moby/buildkit@sha256:7a9e2a8abc3428a555ade8db975f899241d4f5d242fb5599dedd4a0f97e8fc04` (v0.27.1). This is not evidence that a builder exists. `BUILDX_BUILDER` is propagated by the existing executor; no global buildx selection change is needed.
- Standalone JVM diagnostic retry with the owned shared tmpfs passed volume creation and target startup but failed with `kernel quota and actual target-side write required`. Report: `/tmp/nanolab-p24-inputs/standalone-jvm-e20f0ed541974a15944422698d98c332/standalone-validation.json`. Containers and volume were cleaned up without reported errors. Root-cause correction remains pending; the quota/target-write gate was not weakened.
- Full P24 remains unexecuted. Outstanding operator decisions include the dedicated build environment and the missing NanoFaaS population observability. No numeric policy or soak duration has been relaxed.

## Real validation checkpoint: JVM full GC and Node heap snapshot

- This continuation made concrete progress; full workflow/P24 completion remains unproven.
- Helper r2 built and published successfully from the unchanged pinned bases. Reference: `localhost:5000/nanolab/p24-diagnostic-helper@sha256:0e949a61178b9f41a72b6485d6433cccbe2c0d2c9ede3a72d727e3a6c8e5e380`. Build/publish evidence: `/tmp/nanolab-p24-helper-build-r2.XD7Rco`.
- Actual JVM prepare, target-side bounded JFR write, RSS/PSS and cleanup passed in `/tmp/nanolab-p24-inputs/standalone-jvm-25010d97304647f98b4c980583a51103`.
- Actual standalone JVM full GC passed in `/tmp/nanolab-p24-inputs/standalone-jvm-69703704deec4045920ed5829ef2ae8b`. Main inspected the raw event: `jdk.GarbageCollection`, `SerialOld`, cause `Diagnostic Command`, gcId 7, duration 0.007686266 s, correlated to request `a6bd79d92e7344fd8a5c3fc64ae9579f`. Raw JFR is 120187 bytes. This is not a natural-drain checkpoint, heapdump proof or P24 run.
- Node ARM64 target image resolved and pulled: `node@sha256:8d342e46d3b2883df69f797cb60fc71d8a0b65de65ddfbf4bf63fdc02049615f`.
- Actual standalone Node private socket/session, quota, RSS/PSS and cleanup passed in `/tmp/nanolab-p24-inputs/standalone-node-30e15e4711ba4464bfcabe4939419a1b`.
- Actual standalone Node GC and heap snapshot passed in `/tmp/nanolab-p24-inputs/standalone-node-e543286ca29f49c0a80d85247d5e7df9`, with the unchanged 16777216-byte quota. GC evidence reports two correlated `node:perf_hooks:major-gc` events. The complete parsed V8 snapshot is 5616366 bytes, 59105 nodes and 255164 edges. Request, source-event and snapshot references/hashes are retained. All owned containers and the volume were cleaned up; these are standalone capability tests, not a qualifying workload soak.
- Review found and corrections closed two runtime P2s: bounded owned-target JVM argfile capture/argument continuation, and writer close before publishing PASS. Unsupported JVM launch syntax stays unknown. The observation proves launch inputs at sampling, not immutable-from-launch or live ergonomic JVM flags. Independent scoped review found no remaining P1/P2 in those two corrections. Main reran runtime/helper tests: 65 passed in 0.56s.
- JVM heapdump remains unresolved. Both the GC-then-dump case (`standalone-jvm-ad75a93821b04cb59bd580654fbcf791`) and dump-without-explicit-GC case (`standalone-jvm-62534d718d4446ecad1f6bc940980ea2`) failed with unconfirmed remote completion and clean resource cleanup. Neither preserved HPROF or jcmd failure output. ENOSPC is not proven, and the 16-MiB limit was not silently increased.
- Root cause of the missing error evidence was identified: the executor discards the worker's error when raising the generic completion exception. A narrow host-executor change to retain bounded validated failure evidence is in progress; completeness and cancellation requirements must remain unchanged. No identical retest is warranted before that evidence is preserved.
- Rootless alternative feasibility was checked without changing services/security settings: a temporary unprivileged user namespace succeeded, but buildkitd/rootlesskit/newuidmap/newgidmap/slirp4netns were not available on PATH. No rootless installation or privileged BuildKit container was started.
- The existing owned local registry remains the only intentionally retained validation service. Builder approval and NanoFaaS-observability decisions remain outstanding. Full diagnostic wiring into the public P24 runtime is still being assessed; standalone successes do not close that integration requirement. Full P24 has not started.

### Validation checkpoint: r3 diagnostics and public workflow review

- Helper r3 published as `localhost:5000/nanolab/p24-diagnostic-helper@sha256:5ac642accc637de4f0a5621d0804cc5ab784880186f90ea159c1edbd9f7d02ce`; build evidence: `/tmp/nanolab-p24-helper-build-r3.9yfSlZ`.
- Standalone JVM full sequence passed with explicit 64 MiB scratch quota: `/tmp/nanolab-p24-inputs/standalone-jvm-94e8769e538840efbd8be89ee5ea78dc`. Standalone Node full sequence passed with 16 MiB: `/tmp/nanolab-p24-inputs/standalone-node-269921328ece4082a3df821924c06129`. Both reported clean owned-resource cleanup. These are diagnostic probes, not P24 workload evidence.
- JVM 16 MiB negative control remains recorded at `/tmp/nanolab-p24-inputs/standalone-jvm-6e0c526e6afb4edca42c5733891d00b4`: segmented heap merge failure. The 64 MiB positive control produced a complete 11,310,486-byte HPROF. Temporary footprint is implicated; no measured peak or definitive ENOSPC attribution. No P24 acceptance policy changed.
- Main diagnostic suite: 130 passed. Broad soak/plan selection: 758 passed, 1 failed (native preset test bypassed scenario policy loader). Agent corrected that test and reported focused 1 passed in 0.33 s with conftest and coverage excluded; this does not establish a normal full-suite pass.
- Review found three pending public-path issues: automatic diagnostic provider rejected before wiring; natural-checkpoint reads not bounded by the global diagnostic deadline; unavailable-only observations could be accepted as a complete natural checkpoint. Corrections assigned to Franklin.
- Real prerequisite platform factory implementation assigned to Kuhn in a new module and focused tests. Parent recovery must follow worker reap; unresolved ownership must remain an explicit failure/unsupported result. Missing NanoFaaS owner-population observability remains a qualification blocker, not zero or N/A by default.
- Dedicated privileged BuildKit builder has not been authorized or created. No complete app image set, smoke workload, or full qualifying P24 soak has run. Goal remains incomplete.

### Follow-up checkpoint: three public-workflow review fixes validated

Append-only update to "Validation checkpoint: r3 diagnostics and public workflow review". The earlier three-pending-fixes entry remains historical evidence; the three implementation fixes below are now applied and covered by focused tests.

- Automatic preparation support: `PreparationOptions.diagnostic_provider_available` defaults to `False`. The authorized automatic local runtime declares provider availability before preparation; no placeholder adapter or target capability receipt is fabricated. Actual attachment/capabilities remain post-deployment checks.
- Checkpoint deadline: each checkpoint probe receives the lesser of the scrape timeout and the shared diagnostic time remaining. Cancellation is checked within the checkpoint loop. A focused test exercises real subprocess kill-and-wait with a harmless Python child, without running the collector or accessing Docker.
- Observation availability: required metrics and drain-criterion selectors must match the owned target and contain available, finite observations with the required units before completed natural markers or any GC. Missing observations are not zero-filled or promoted to PASS.
- Latest focused result: **112 passed in 0.87s** across `test_runtime.py`, `test_preparation.py`, `test_retention_records.py`, and `test_compose_retention.py`. Ruff passed for the four files changed in this fix slice.
- Effective pytest flags: `--confcutdir=tests/soak -o addopts='' -q --tb=short`, from `packages/nanolab`, using the shared virtualenv and pinned cached Sonata paths in `PYTHONPATH`. The parent conftest that invokes Git was excluded.
- Permission state: diagnostic target-stop API defaults to `False`; the public owned-soak builder explicitly supplies `True`. Memory-only target-stop permission remains `False`. No Git or Docker was executed by this agent. Privileged BuildKit remains **not authorized**.
- Review status: Euler's partial review reported no findings for provider/capability separation and sample validation. Its initial runtime read was truncated; collector behavior and marker/GC ordering have NOT received a complete Euler review. Do not record this as a complete independent review.
- Integration status: awaiting Kuhn's stable real prerequisite factory/runner API and parent recovery-after-reap contract. No placeholder integration has been added. Existing public connector: `PreparationOptions.prerequisite_runner`.
- Qualification boundary: synthetic tests and reported standalone r3 diagnostic successes do not establish an integrated qualifying P24 run. Missing observability or prerequisite evidence remains a blocker; no criteria or budgets were relaxed.

### Prerequisite factory checkpoint and sandbox failure isolation

- Added `tasks/soak/prerequisite_platform.py` and `tests/soak/test_prerequisite_platform.py`; implementation remains unvalidated and is not yet wired into the public runner. No live deployment or soak evidence is claimed.
- Main reproduced first focused test stalling with an external 25-second timeout (exit 124). Trace shows workflow holding release and asyncio event loop waiting. Test selected with `--noconftest`, project importlib mode, cache disabled; no Git/Docker.
- Independent minimal control, unrelated to NanoLab: `asyncio.run(asyncio.to_thread(lambda: 1))` also timed out after 5 seconds (exit 124).
- Further minimal control: `socket.socketpair()` succeeds but sending one byte raises `PermissionError: [Errno 1] Operation not permitted` in the sandbox. This provides direct environmental evidence affecting cross-thread asyncio wakeup; no product workaround or passing test is inferred.
- Both main bounded test/control sessions ended. A previous agent test session has an unknown handle and cannot be declared reaped from the sandbox-local process listing.
- Previous agent reports an outside-sandbox attempt aborted by user. No repeat escalation issued; explicit approval is needed before retrying this focused validation outside the sandbox.

### Focused prerequisite checks without thread wakeup

- Main ran the new factory test selection `reject_mismatched or recovery_partial or recovery_refuses`: 8 passed, 6 deselected in 0.15 s, bounded by 20-second external timeout. Used `--noconftest`, importlib mode, cache disabled. These checks do not exercise complete platform lifetime acquisition/release.
- Initial Ruff check on the two new files failed with 96 findings. Agent reports style-only correction and subsequent Ruff PASS; no semantic change or outside-sandbox test was made.
- Public provider/factory/reap-recovery wiring is assigned and remains in progress. Factory artifact allocation is currently per lifetime; parent cumulative reservation including failed acquisition and recovery remains required before claiming compliance with the run artifact limit.

### Public prerequisite integration checkpoint: not ready for soak

- Latest patch applied to `runtime.py`, `preparation.py`, and `prerequisites.py`: provider declaration before build; postbuild factory/runner with lazy imports; parent cumulative reservations before fork; recovery following supervisor reap; stop subsequent profiles when cleanup is unconfirmed.
- Functional validation of this patch has NOT run. Earlier 112 PASS and factory selection 8 PASS do not cover this integration. Main static check initially found 30 Ruff findings; agent made style-only corrections and reports Ruff PASS on all three files.
- Factory enforcement of the complete global artifact limit remains unresolved: parent reservations do not bound every executor log, ownership/release journal and generated file. Integration is not qualifying and must not be presented as a finished workflow.
- All assigned implementation agents report stopped, with no active sessions from their latest work. Historical unknown agent test handle remains unconfirmed; the owned local registry remains intentionally retained. No privileged builder or actual soak workload has started.
- Further functional validation is blocked on explicit permission to rerun the bounded thread-based tests outside the sandbox after the prior user-aborted attempt. Full image preparation separately needs explicit permission for privileged BuildKit; P24 qualification also requires resolution of missing authoritative observability and frozen policy. These pending permissions have recurred across multiple goal turns; no consent is inferred from automatic continuation messages.

## 2026-09-13 — Workflow runs end to end; P24 blocked on platform instrumentation

The public soak workflow now executes completely against real Docker, from
source capture through build, deploy, preflight, measurement, evaluation and
teardown. The **full P24 soak was NOT started**, because its prerequisite
coverage cannot be satisfied by the current NanoFaaS instrumentation. That is
a measured finding, recorded below, not an inference.

### Sandbox blocker cleared

`asyncio.to_thread` and cross-thread socketpair wakeup both work in this
session's sandbox. `tests/soak/test_prerequisite_platform.py` runs unmodified:
14 passed. The previously pending outside-sandbox permission is not needed.

### Defects found by running the workflow, and fixed

Each was found by an actual run, and each has a regression test.

1. **Stale test stubs hid a live path.** Three `SimpleNamespace` scenarios and
   one `prepare_soak` stub predated the prerequisite wiring, so six
   `test_runtime.py` cases failed on `config.prerequisites`. The stubs were the
   defect; `SoakConfig.prerequisites` is required.
2. **Bake interpolates `dockerfile-inline`.** The injected Node preload keeps
   the caller's `${NODE_OPTIONS:-}`, which HCL rejects outright, and any other
   `${VAR}` would have been substituted away silently. The bake value is now
   escaped (`$${`); the Dockerfile written beside the evidence is unchanged.
   Covered by a test that runs a real `docker buildx bake --print`.
3. **The control plane could not write its catalog.** Containers run as the
   host user so procfs stays readable, but the image ships `/var/lib/nanofaas`
   owned by its distroless user. The platform never became ready. It now gets a
   run-owned writable directory at that declared path, and nothing else.
4. **Function registration used the wrong field name.** `FunctionSpec` names it
   `endpointUrl` and rejects unknown properties, so every registration was a
   400. Confirmed against the live API before and after.
5. **k6 `inspect` does not read the process environment.** Only `k6 run` does,
   so the generator inspection failed with an empty `open()` filename. The
   config is now named on the command line.
6. **Spring Boot endpoint access, not exposure.** The image entrypoint carries
   `-Dmanagement.endpoints.enabled-by-default=false`, so exposing
   configprops/info left them at 404 and the effective retention and module
   observations were unavailable. Per-endpoint read-only access is now set.
7. **Modules were read from the wrong endpoint.** Only the `build-metadata`
   module reports the compiled module list, at `/modules/build-metadata`;
   `/actuator/info` never carried it. The soak scenarios now declare that
   module and the observation reads it there.
8. **`runtime_options` compared two different things.** The declared value is
   what the soak injects; the observation is the complete launch option list,
   which always includes the image's own flags, so equality could never hold.
   It is now an ordered-subsequence check: every declared option must really be
   in effect, in order.
9. **The artifact inventory could not fit in one record.** It enumerated all
   5,520 files of the source tree, overflowing the 1 MiB record bound. The tree
   is already hash-inventoried by `source-manifest.jsonl`, which
   `snapshot.json` seals, and both stay in the inventory.
10. **Teardown used the cancellation budget verbatim**, timing out mid-stop and
    leaving the platform up under "cleanup unconfirmed". Services now declare
    their stop grace and cleanup budgets past it.

### Cross-clock comparisons that no real run could satisfy

Six acceptance checks compared independently-read clocks with exact equality or
strict ordering. All are now bounded by one sampling interval — small enough
that no observation can hide in the slack, and stated as such in the code.

- Phase contiguity required `start == previous`; real transitions differ by
  milliseconds.
- `frozen_at_s <= builds_finished_s` had the order backwards: images are frozen
  *after* the builds that produce them, which is what `PreparedSoak` records.
- Observation start/end and `traffic_stopped_s` were compared exactly.
- Workload start/end were compared exactly against the phase boundaries.
- `required_sample_count` used `ceil` over a float duration marginally above
  the declared one, demanding a sample that alignment cannot guarantee; the
  achievable minimum is the floor. Gap checks, not the count, catch real gaps.
- Admission boundaries required a sample scheduled at the exact window edge.
  They now have to bracket the window within one interval, strictly, so a whole
  missing observation still fails.
- Admission reconciliation required `success <= admitted`, which the counter's
  tick sampling makes impossible: it cannot see the traffic in the edge
  slivers. It now allows exactly that much at the declared rate, and no more.

The two guards that must reject a tampered admission count (`gap`,
`success-count`) still fail as intended; this was verified after the change.

### Artifact budget now actually bounded

`measure_tree`/`enforce_limit` measure every generated file under the run
directory — journals, command logs, k6 output, diagnostic dumps — and the
observer checks the total once per sweep, so a run that would exceed its budget
stops while its evidence is still valid. Acceptance additionally measures the
tree, so a file written past the inventory cannot hide. A build-from-source
run measures ~392 MiB, against P24's 8 GiB.

### Smoke result

`/tmp/nanolab-p24-inputs/smoke-arm64.yaml` runs the complete workflow in ~3.5
minutes plus build. **14 of 19 gates PASS**, including frozen-policy,
source-artifact-provenance, effective-preflight, workload-correctness,
workload-accounting, prerequisite-coverage, diagnostic-coverage,
required-observations, sample-integrity, run-continuity and artifact-integrity.
Re-verified end to end after the lint and docstring cleanup.

The remaining INCONCLUSIVEs are the designed review path, not defects: the
smoke's `growth_review` threshold is 0, so any RSS growth after a 60 s load and
45 s drain requests attribution, and `attribution-policy-binding` and
`run-coverage` follow from that.

The smoke's original cadence was unachievable — it declared a 1 s interval when
one sweep of three roles costs ~1.5 s, so the observer ran at 3 s and every
window check failed. Its phases and interval were corrected; P24's 10 s
interval was already feasible and is unchanged.

### P24 is blocked: the required populations are not instrumented

P24 declares five prerequisite profiles. `run_prerequisites` requires, for
every image role, authoritative settlement observations of `live_executions`,
`payload_bytes`, `timers` and `pending_http`, plus `callbacks`,
`idempotency_entries`, `retired_owners` and `metric_series` for the profiles
that need them. Measured from a real run's preflight (`preflight.json`,
`observations.roles.*.metrics`):

| Population | control-plane | word-stats-java | word-stats-javascript |
| --- | --- | --- | --- |
| live_executions | logical only | servlet gauge only | `runtime_active_handlers` |
| payload_bytes | **none** | **none** | `runtime_input/output_bytes` |
| timers | **none** | **none** | **none** |
| pending_http | pool gauges only | **none** | pending callbacks only |
| callbacks | **none** | failures only | `runtime_pending_callbacks` |
| idempotency_entries | `idempotency_keys_held` | n/a | n/a |
| retired_owners | **none** | **none** | **none** |

`prerequisite_runtime.py` states in its own defaults that the control plane's
`execution_in_flight_records` is logical state and the Java SDK's
`runtime_in_flight` is a servlet request gauge — neither is physical handler
work, and `_DEFAULT_METRICS["java"]` is empty. Binding them would be a
relabelling, which is exactly what the module refuses.

Its header also records that the fault profiles need real configured
fault/delay handlers, and that the shipped warm-echo handler cannot prove
`error-timeout-cancellation` or `async-late-callback`.

Two distinct pieces of work therefore remain before P24 can run:

- **NanoFaaS (authorized, not started):** gauges for the missing populations in
  the control plane and the Java SDK, `timers` and `pending_http` in both SDKs,
  `retired_owners` for function-name churn, and fault/delay-capable handlers
  for the two fault profiles. None of this was begun: a gauge that retains a
  reference would itself create the leak P24 measures, so it needs writing and
  reviewing properly, not hurriedly.
- **NanoLab:** the public plan never builds `prerequisite_inputs`, the
  parent/lifetime/recovery quotas or the acquire/release timeouts, so
  `_check_prerequisite_wiring` fails at construction with "automatic
  prerequisites require explicit frozen recipe inputs". The full-subsystem
  smoke (`smoke-full-arm64.yaml`) stops there, before any container starts.

No criterion, duration or budget was relaxed to work around either. Running
P24 now would fail in its first minute, or produce a run that cannot qualify.

### P24 inputs prepared and validated

`/tmp/nanolab-p24-inputs/p24-arm64.yaml` plus the operator policy
`memory-soak-policy.yaml` now compile through the real loader:

- one version, JVM on ARM64; measurement 9,840 s = 2 h 44 m plus build;
- 5 CPU and 2.5 GiB across the three roles, matching the declared limits;
- RSS returns to the baseline median with `absolute_tolerance` and
  `relative_tolerance` both 0 — no positive residual — evaluated over the last
  300 s of the 2,100 s drain, after the 1,800 s retention has lapsed;
- per-role steady memory ceilings at the allocated limits, as the schema
  requires;
- artifacts capped at 8 GiB; diagnostics pinned to the validated r3 helper.

### Gate status

- Full suite: **2,364 passed, 2 failed** — the two failures are the pair
  CLAUDE.md documents against a checkout that does not match the pinned
  nanoFaaS revision, and both fail identically on the base commit.
- `ruff check packages`: **clean**. `ruff format --check`: **clean**.
  Two per-file-ignores were added, each with its reason: the container-side
  diagnostic worker, and the three files that embed Gradle/Node source.
- `lint-imports`: **3 contracts kept, 0 broken**.
- `basedpyright`: started from **337 errors** (206 src, 131 tests) and now at
  **70** (11 src, 59 tests). See the type-checking section below.

### Environment

The temporary privileged BuildKit builder was created pinned by digest
(`moby/buildkit@sha256:7a9e2a8a…`, v0.27.1), limited to 16 GiB and 8 CPUs, used
for every build, and **removed**. Attestations were kept; the Docker daemon was
not modified. The owned local registry is still up: it holds the validated r3
diagnostic helper the P24 scenario pins. No soak containers remain.

## 2026-09-13 — Type checking: 337 errors down to 70

The base commit is clean, so every error belonged to the new soak code. The
full suite stayed at 2,364 passed / 2 pre-existing failures after every batch,
and ruff, ruff format and import-linter stayed clean throughout.

Most of the count came from four structural causes rather than from many
independent mistakes:

- **Predicates that did not narrow.** `_finite`, `_count`, `_number` and
  `_positive` answer "is this a finite measurement?", and every caller goes on
  to compare or convert the value. Declaring them `TypeGuard` narrows at all
  ~50 call sites. Alongside it, `type(x) in (int, float)` does not narrow while
  `type(x) is int` does; the `in` form was replaced everywhere it appeared. The
  bool-excluding semantics are unchanged — `isinstance` would have accepted
  booleans as measurements.
- **`read_records` returned `dict[str, object]`.** These are JSON records, and
  `_json` already said `dict[str, Any]`; making the two agree removed a large
  batch of `reportArgumentType` on sample fields.
- **`_require(config is not None, …)` does not narrow a closure variable.** The
  acceptance gates each carry an `assert` after the `_require` that already
  fails closed. The assert is for the checker only: under `-O` it disappears
  and `_require` still raises, so behaviour is identical. `acceptance.py` went
  from 114 errors to 2.
- **Untyped test doubles.** The project's existing idiom is
  `cast(Type, SimpleNamespace(...))`; the soak tests now follow it, through
  small typed factories (`fake_deployment`, `fake_config`, `host_bindings`)
  rather than a cast at every call site.

A few genuine slips surfaced on the way: `_admitted`'s return annotation never
followed the change that added the uncounted span, and `RuntimeDeployment`'s
endpoint map was built as `dict[str, str]` against a `dict[str, str | None]`
field.

### What is left, and why it is harder

11 in src, 59 in tests. The src remainder is structural rather than mechanical:

- `class MemoryOwnedLifecycle(lifecycle_type)` — a dynamically chosen base
  class, which a static checker cannot follow.
- `PrerequisitePlatformFactory.assign_lifetime_budget` and
  `CommandTaskExecutor.observe` — attributes used on a Protocol that does not
  declare them. The honest fix is to widen the Protocol, not to cast.
- `HoldPlatform`/`DeferredMeasurement` against `Task`, and one incompatible
  `run` override — the Task contract wants a stricter signature than these
  local subclasses provide.
- `_OfflineSink` standing in for `ArtifactWriter` — the same duck-typing
  question, in src rather than in a test.

Each of these is a real design question about the contracts, not an annotation
to add, so none was papered over with `# type: ignore`. The test remainder is
more of the same cast work already applied elsewhere.
