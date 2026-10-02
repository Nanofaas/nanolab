# P24 Container Recipe Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare the canonical P24 container preset through one recipe publication while preserving its experimental protocol and acceptance boundaries.

**Architecture:** Extend the existing strict soak recipe validators to the supported P24 JVM/Node selection. Add a dedicated profile and reference it from the canonical preset. Reuse snapshot preparation, publication, observed provenance and frozen receipts; retain existing runtime/prerequisite/evaluation behavior.

**Tech Stack:** Python 3.12+, uv, existing Sonata pins, Gradle recipes v2, native ARM64 Linux Docker/Buildx, local registry, existing soak observers.

**Spec:** `docs/superpowers/specs/2026-10-02-soak-p24-recipes-design.md` (approved; current soak selection contract corrected during plan review).

## Global Constraints

- NanoLab base `4689469`; independent branch `feature/recipe-soak-p24`. NanoFaaS pin `e7914be0`, version `0.22.0`; `NANOFAAS_ROOT=/tmp/nanofaas-release-arm64-pin`. Verify this clean checkout still exists before running dependent commands. No plugin/source/dependency change.
- Scope: only `memory-soak-sync-container.yaml` preparation. Preserve existing smoke, non-recipe consumers and other presets. Containerd, native variants, other P24 presets and acceptance-policy revisions remain separate.
- Profile `soak-container-p24-jvm.yaml`: JVM control plane, Java JVM word-stats and JavaScript word-stats; modules exactly container-deployment-provider, async-queue, build-metadata; `linux/arm64`; repository `127.0.0.1:5000/nanofaas`; provenance true; unique actual run tag. No new explicit JVM/GC/compiler/build overrides.
- Canonical protocol unchanged: P24, advanced metrics; phases 120/2100/120/5400/2100/300 seconds; rates20/20, VUs200/200, zero errors/drops; all existing resources, retention, diagnostics, prerequisite coverage, criteria and evidence budgets.
- One dirty-inclusive immutable snapshot, one `publishRecipe`, three exact role receipts; no preceding assembly, additional legacy application builds or fake prerequisites. Original/staging revisions remain distinct.
- Independent registry/index/manifest/config verification and actual-context compiler/base/toolchain observations remain mandatory. Preparation/probes/smoke do not set `p24_qualified: true`.
- Keep existing uninterrupted soak contract: reject resume/partial selection, require unused run-dir, evaluate saved evidence offline. No new `--fresh`/resume API or receipt conversion.
- Real verification uses Docker on native ARM64 host; no VM, Azure or emulation. Do not launch the multi-hour P24 campaign automatically. A blocked preparation gate remains open and is documented.
- Owned compensation and diagnostics preserved; no cleanup of unrelated containers/images, no write to the operator NanoFaaS checkout, no public push/signing/scheduled action.

## Review Focus

1. A valid-looking P24 profile changes JVM launcher/GC options although scenario overrides are empty: reject policy drift and compare effective settings, rather than silently adopting the smoke profile's override (Tasks1/3).
2. A raw preset consumer calls `ScenarioConfig.model_validate` without resolving relative recipe/policy paths: migrate the actual loader consumers and fixtures, preserving strict rejection of unresolved inputs (Task1).
3. A partial or mutated recipe publication reaches prerequisite/measurement code: no complete receipts, no deployment/measurement, retain diagnostics and compensate only owned resources (Task2).
4. Provider wiring is confused with successful prerequisite coverage: preparation may declare real wiring, but no synthetic success receipt or smoke evidence may qualify P24 (Tasks2/3).
5. An old legacy run or altered profile/report is evaluated offline: never relabel it as recipe evidence, republish while checking, or claim resumable measurement (Task2).

## Task 1: Admit the exact P24 recipe selection and migrate its preset

**Files:** Create `packages/nanolab/recipes/soak-container-p24-jvm.yaml`; modify `packages/nanolab/scenarios-v2/memory-soak-sync-container.yaml`, `packages/nanolab/src/nanolab/config/scenario.py`, `packages/nanolab/src/nanolab/tasks/soak/recipe.py`, `.github/workflows/ci.yml`; tests `packages/nanolab/tests/soak/test_recipe.py`, `packages/nanolab/tests/soak/test_presets.py`, `packages/nanolab/tests/plans/test_soak.py`, `packages/nanolab/tests/test_soak_cli.py`.

**Interfaces:** Preserve `ScenarioConfig.validate_workflow(self) -> ScenarioConfig`, `validate_soak_recipe(profile: Path, config: SoakConfig, *, platform: str) -> None` and existing `_scenario(path)` resolution. Admit `p24` alongside `smoke` only for the existing local container JVM/Node build contract; no new runner or public fields. Later tasks consume the canonical `_scenario` result and its absolute `recipe_profile`.

- [ ] Write `test_p24_recipe_preset_preserves_experiment`: resolve the canonical scenario through `_scenario`, compare its policy-resolved baseline from base `4689469` (fixed fixture, no dependence on a moving branch). Remove only `recipeProfile` before comparison; assert identical remaining configuration. Assert purpose/profile `p24`/`advanced`, phases120/2100/120/5400/2100/300, rates20/20, VUs200, and the existing policy criteria/prerequisite/diagnostic fields. Assert exactly the new resolved profile path.
- [ ] Add `test_p24_recipe_accepts_supported_profile` and `test_p24_recipe_rejects_policy_drift_before_acquisition`: valid strict P24 fixture through normal policy/model validation; reject profile JVM args/GC overrides, missing provenance, platform mismatch, wrong/duplicate modules, foreign/missing function, native/prebuilt build, services and inconsistent role expectations. Keep the smoke's valid explicit launcher configuration accepted. Replace the old test treating every P24 purpose as unsupported with genuinely invalid P24 cases; never bypass model duration validation via `model_copy` to prove P24 support.
- [ ] Add `test_p24_recipe_loader_preserves_path_and_policy_guards`: working-directory-independent relative recipe/policy resolution; missing/invalid policy or profile stops before any provider/build/source call. Add short P24 duration rejection and tests keeping containerd/Kubernetes/native and conflicting scenario overrides rejected. Adapt existing raw-preset fixtures to use the actual resolver when appropriate; do not weaken the absolute-path requirement.
- [ ] Add `test_p24_recipe_plan_is_deferred`: compile the canonical preset with external-call-failing doubles; assert recipe path reaches preparation, exactly the existing registry/builder resources, unchanged prerequisite/runtime options, and no source capture/publication while planning.
- [ ] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/soak/test_recipe.py packages/nanolab/tests/soak/test_presets.py packages/nanolab/tests/plans/test_soak.py packages/nanolab/tests/test_soak_cli.py -q --no-cov`. Expected: new P24 tests fail on current purpose rejection/profile absence; baseline guards remain valid.
- [ ] Add the profile with no explicit JVM launcher/GC/build overrides; choose image names `soak-p24-control-plane`, `soak-p24-java-word-stats`, `soak-p24-javascript-word-stats` and manual tag/name `soak-container-p24-jvm`. Add only `recipeProfile: ../recipes/soak-container-p24-jvm.yaml` to the canonical YAML plus accurate comments. Extend the two existing purpose guards and P24 option validation minimally; retain supported smoke behavior and all existing role/matrix checks. Add the profile to CI's explicit pinned `validateRecipe` checks. Preserve dependency pins and policy file bytes.
- [ ] Rerun the focused command. Expected: PASS. Extract a fresh immutable pinned NanoFaaS archive under `/tmp/nanolab-soak-p24-recipes-verification/gradle-source/` and run `./gradlew validateRecipe -Precipe=<absolute-new-profile>` there, redirecting output to `validate-recipe.log`. Expected: `BUILD SUCCESSFUL`, three intended components, operator checkout clean. Native effective runtime equivalence is still a separate Task3 gate.
- [ ] Run `git diff --check`; expected clean. Commit `Use recipes for the P24 container preset` with the profile, preset, validators, CI and tests.

## Task 2: Prove preparation, failure and qualification contracts

**Files:** Tests `packages/nanolab/tests/soak/test_preparation.py`, `test_recipe_observation.py`, `test_recipe_registry.py`, `test_runtime.py`, `test_prerequisite_inputs_wiring.py`, `test_report.py`, `packages/nanolab/tests/test_soak_cli.py`; modify only implicated existing production boundaries if a new regression exposes a gap: `tasks/soak/preparation.py`, `recipe.py`, `runtime.py`, `recipe_observation.py`, `recipe_registry.py`, `cli/soak.py`.

**Interfaces:** Reuse unchanged `prepare_soak(config: SoakConfig, *, run_dir: Path, repo_root: Path, tool_root: Path, options: PreparationOptions | None = None, build_observer: Any = None, cancelled: Event | None = None) -> PreparedSoak`; `publish_soak_recipe(snapshot: SourceSnapshot, profile: Path, config: SoakConfig, *, run_dir: Path, tag: str, builder: str, executor: OwnedBuildCommandExecutor, artifact_limit_bytes: int) -> tuple[BuildReceipt, ...]`; `observe_soak_recipe(...)` and existing registry readers. Use `PreparedSoak.snapshot`, `.receipts`, `.images`, `.writer` downstream; always close the writer. Existing prerequisite inputs/coverage evaluation remain authoritative.

- [ ] Extend preparation/observer fixtures with a fully model-validated P24 config. Add `test_p24_preparation_publishes_one_snapshot_without_legacy_build`: source capture count1, publication command count1, exact three role receipts with same source fingerprint, original recipe identity and distinct verified registry/config identities; `recipe.bake is None`; legacy `BuildImagesTask` never invoked. Verify original dirty/untracked inputs and separately identified staging/instrumentation through real filesystem handling; doubles belong at Gradle/Docker transport boundaries, not internal domain functions.
- [ ] Add `test_p24_preparation_failure_never_reaches_measurement`, parameterized for incomplete report, profile/source mutation, failed command, registry digest/platform mismatch and cancellation after publication starts. Assert no completed preparation/runtime success, no deployment/prerequisite/measurement calls, retained failed diagnostics and owned compensation. Extend existing builder-selection cleanup test to the canonical P24 configuration; preserve unrelated resources.
- [ ] Add `test_p24_recipe_prerequisite_inputs_bind_frozen_receipts`: existing prerequisite-input freezer receives the new source/config/three digest-pinned role receipts; absent coverage remains incomplete and smoke receipts rejected. Assert provider-availability declarations alone never produce prerequisite success or `p24_qualified: true`. Do not fabricate successful owner coverage to reach measurement.
- [ ] Add `test_p24_recipe_saved_evidence_is_not_resumed_or_republished`: `--resume`, `--only`, `--from`, `--until` rejected before external work; used run-dir rejected; `soak-evaluate` performs no publication/build and preserves incomplete or failing verdicts. Altered profile/report/source references must not validate as original recipe evidence; original legacy evidence remains its original format and cannot claim recipe preparation.
- [ ] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/soak packages/nanolab/tests/plans/test_soak.py packages/nanolab/tests/test_soak_cli.py -q --no-cov`. Expected: explicit guards PASS if existing shared behavior already satisfies them; any exposed defect must first be reproduced RED before changing its production boundary.
- [ ] Fix only demonstrated defects at their shared root, preserving the interfaces above and existing protocol. If all contracts already pass, keep this task test-only. Rerun the same focused command; expected PASS. Commit `Verify P24 recipe preparation and evidence barriers`.

## Task 3: Verify preparation with native Docker and smoke regression

**Files:** Retain private verification scripts/logs/artifacts only under `/tmp/nanolab-soak-p24-recipes-verification/`; no product runner or committed generated evidence. Update this plan's actual gate record as results arrive.

**Interfaces:** Consume Task1's canonical resolved config/profile and Task2's shared `prepare_soak` contract. Use existing registry/Buildx resources and `RuntimeOptions`/`_runtime_preparation_options` wiring for real preparation capabilities; the latter declares provider wiring and never proves target/prerequisite success. This is an isolated preparation harness, not a partial public soak run.

- [ ] Record baseline Docker containers/image IDs, selected builder, daemon OS/architecture and any pre-existing registry. Expected: native Linux ARM64; otherwise the real gate stays incomplete without emulation or platform edits. Verify the clean pinned NanoFaaS checkout and policy/profile/preset input identities relevant to this slice; retain their hashes, not secrets.
- [ ] Create a bounded private harness using the existing owned resource lifecycle: validate the full canonical P24 config, acquire its registry and selected-builder-restoration wrapper, and wire actual default prerequisite/diagnostic providers using production preparation options. Call `prepare_soak` with the new profile, owned builder and required local registry, then close its writer and release owned resources in `finally`. Never bypass support checks with a no-op, fabricate adapters/receipts, or call the multi-hour measurement task. Expected: real preparation produces three complete immutable receipts or reports a concrete missing-capability gate before proceeding.
- [ ] Retain the one real `publishRecipe` command/full logs, raw profile/distribution, dirty-inclusive source identity, observation/provenance and registry artifacts. Independently inspect executable ARM64 manifests/config IDs and actual effective JVM/Node settings against existing preset/default build expectations using read-only catalogue planning and actual observations; no second application build. Expected: three verified roles, one source/publication, no policy drift. A compiler/launcher mismatch stops qualification and requires an explicit documented resolution.
- [ ] Check owned cleanup against the baseline, including selected builder and unrelated image/container IDs. Expected: owned resources released, unrelated resources intact, evidence retained. Missing/failed checks are incomplete, not inferred from mocks.
- [ ] Run `NANOFAAS_ROOT=/tmp/nanofaas-release-arm64-pin uv run --locked --package nanolab nanolab run packages/nanolab/scenarios-v2/memory-soak-smoke-recipe-container.yaml --run-dir /tmp/nanolab-soak-p24-recipes-verification/smoke-run` with logs retained as `smoke.log`. Expected: unchanged recipe smoke completes preparation/deployment/measurement/report/cleanup; record the real numerical verdict, including INCONCLUSIVE if unchanged limits still fail. This run remains smoke and `p24_qualified: false`. Run its matching `--teardown` after success or failure and verify ownership; do not change thresholds to obtain exit0.
- [ ] Record preparation and smoke integration separately from numerical acceptance and genuine P24 coverage. Expected: PASS or explicit incomplete per gate; no P24 qualification claimed. If native work is blocked, keep this task's live gates unchecked and continue truthful local documentation/review.

## Task 4: Final checks, documentation and whole-branch review

**Files:** Modify `README.md`, `packages/nanolab/recipes/README.md`, `docs/soak.md`, `docs/recipes-roadmap.md`, this plan.

**Interfaces:** Consume the actual Task1–3 verification results. No new product behavior. Native inline execution is the preserved user preference; one fresh whole-branch final reviewer, no task-by-task reviewer delegation.

- [ ] Run `NANOFAAS_ROOT=/tmp/nanofaas-release-arm64-pin uv run --locked --package nanolab pytest packages/nanolab/tests -q`; expected complete suite PASS. Run `uv run --locked --package nanolab nanolab-quality`; expected `Quality checks passed`. Run `uv run --locked --package nanolab ruff format --check --config packages/nanolab/pyproject.toml packages/nanolab`; expected formatted. Run Bandit with `-r packages/nanolab/src/nanolab -c packages/nanolab/pyproject.toml -f json -o <task-workspace>/bandit.json`; expected only four existing B101 LOWs, retain exit1 and report any regression. Run `git diff --check`; expected clean.
- [ ] Document canonical P24 profile/preparation and real evidence scope. Correct stale prose claiming the policy is unshipped, 100requests/s per function or AMD64 where the actual preset has a shipped policy,20/20rates and ARM64. Document unchanged uninterrupted lifetime/no resume, offline evaluation and native preparation versus multi-hour acceptance. Do not mark the broad P24/backend roadmap item fully complete; add this slice's actual preparation result and leave other presets/backends and qualification pending.
- [ ] Self-check every spec requirement against tasks/tests and actual evidence. Review the whole branch with a fresh reviewer per `executing-plans`/`requesting-code-review`, including Review Focus and any rulings. Expected: no unfixed Critical/Important findings; retain minors/rulings and native limitations. Fix actual Important/Critical findings in one RED→GREEN pass and rerun the full suite.
- [ ] Commit `Document P24 recipe preparation verification` if native preparation passed; otherwise `Document P24 recipe preparation verification gates`. Update checked steps to actual results only. Merge/push remains a later user integration choice.

## Plan self-review and handoff

Coverage: Task1 owns exact profile/admission/canonical-policy preservation and
non-executing resolution; Task2 owns snapshot/publication/evidence/prerequisite
and failure contracts; Task3 owns real native preparation/effective settings,
compensation and smoke regression; Task4 owns fresh suite/quality/documentation
and final review. Every Review Focus entry has a named test or live check.
Existing interfaces are preserved across tasks; no new Task/type is required.

Plan-review correction: the original spec's completed-run resume/`--fresh`
phrasing did not match `validate_soak_selection`, which rejects all resume and
partial measurement. The spec now explicitly preserves the existing refusal,
unused run-dir and offline evaluation. No implementation or policy was changed.

Execution preference: native inline in this session, as previously requested.
Review this written plan before implementation begins.
