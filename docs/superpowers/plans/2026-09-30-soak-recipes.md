# Container Smoke Soak Recipes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and publish the three container smoke application images through one recipe invocation, preserving immutable source identity and observed provenance through a complete host Docker soak.

**Architecture:** Recipe selection enters the existing deferred preparation boundary. A soak-specific adapter stages the existing source snapshot, executes one publication and converts verified distribution, registry and observation evidence into existing `BuildReceipt` values. The frozen deployment and measurement continue to consume `PreparedSoak`.

**Tech Stack:** Existing Python/uv workspace, Sonata resources and owned command executor, NanoFaaS Gradle recipes v2, Docker Buildx, local Registry v2, existing soak observers and k6. No new Python dependency or generic infrastructure task.

**Spec:** `docs/superpowers/specs/2026-09-30-soak-recipes-design.md`

## Global Constraints

- Use Docker on the host; no Multipass VM or emulation.
- Pin verification to NanoFaaS `e7914be065e844776af57fe9e449bce7f12e03c5`.
- Exactly one dirty-inclusive source snapshot and one application `publishRecipe`; no application build after freeze.
- Application roles: JVM control plane, Java JVM word-stats, JavaScript word-stats.
- Control-plane modules: `container-deployment-provider`, `async-queue`, `build-metadata`.
- Registry: `127.0.0.1:5000/nanofaas`; unique run tag and explicit owned builder.
- Preserve maximum BuildKit provenance and actual toolchain/base-image observations.
- Deploy verified executable manifest digests, keeping index/configuration identity separate.
- Preserve phase durations, workload rates, criteria, cancellation, evidence limits and ownership contracts.
- Keep `purpose: smoke` and `p24_qualified: false`; retain the existing smoke scenario.
- No native, multiarch, other backend, release, signing or scheduled-action changes.
- Never weaken evidence requirements to get a successful smoke result.

## Review Focus

1. Dirty/untracked source inputs lost by Git-based recipe staging: preserve inventory and reject mutation (Task 2).
2. Attestation index mistaken for the executable image: verify descriptors and bind provenance to the runtime manifest (Task 3).
3. Version output from the observer's PATH mistaken for the actual compiler: require request-bound successful build-context records (Tasks 1 and 4).
4. Partial publication, cancellation or evidence quota exhaustion leaving owned processes/resources: retain partial evidence and compensate (Tasks 4 and 5).
5. Recipe selection silently overriding role/platform policy or rebuilding on restart: reject contradictions before acquisition and retain single-entry runtime behavior (Tasks 2 and 5).

## Working context and commands

Branch: `feature/recipe-soak`, based on NanoLab `36f5925`. The existing isolated
worktree is `/tmp/nanolab-recipe-multiarch`; its name reflects the preceding
completed feature. Do not modify the unrelated dirty files in the primary repo.

Set `NANOFAAS_ROOT` to the verified pinned checkout. Every test command below
uses `uv run --locked --package nanolab pytest`; use the same environment for
the product CLI. Local-server/full-suite tests require access to local sockets.
Record exit status and retained command logs; do not infer success from a tail.

## File responsibilities

- `config/scenario.py`: allow the supported recipe soak and retain strict exclusions.
- `tasks/soak/recipe.py` (new): profile compatibility, snapshot staging, owned publication and receipt adaptation.
- `tasks/soak/recipe_registry.py` (new): verify attested single-platform registry artifacts; reuse existing HTTP/digest helpers.
- `tasks/soak/recipe_observation.py` (new): bridge the effective recipe build to existing observed-provenance checks.
- Existing `build_observation.py` and `build_provenance.py`: share compatible evidence helpers, retaining legacy behavior; avoid wholesale refactoring.
- Existing `preparation.py`, `runtime.py`, `plans/soak.py`: pass recipe selection through deferred preparation and owned resources.
- One new recipe/scenario and the existing CI profile gate: reusable entry point.
- Soak tests and documentation: contract verification and operational evidence.

## Task 1: Establish the publication observation capability gate

**Files:** Read NanoLab `tasks/soak/{build_observation,build_provenance,build_executor}.py`, `tasks/recipe.py` and the pinned NanoFaaS recipe implementation. Update this plan with the capability decision and evidence paths; keep probe inputs/output outside product source.

**Interfaces:** Consumes the pinned NanoFaaS recipe implementation and current soak evidence contract. Produces a documented mapping from actual recipe execution to maximum provenance, per-role effective compiler/Node/Gradle/BuildKit records, material digests and publication identities. It must identify the concrete observation mechanism before Task 4.

- [x] Inspect NanoFaaS recipe flow through GitNexus query/context first, following its repository AGENTS instructions; confirm index freshness and read the pinned implementation for exact behavior. Do not edit NanoFaaS or its plugin.
- [x] Record how `publishRecipe` selects JVM compilation, JavaScript Dockerfile/build stages, builder, provenance mode and per-component output metadata. Inspect generated-task behavior, not only schema acceptance.
- [x] Make a disposable materialized snapshot/profile probe for the exact three roles. Determine whether existing supported Gradle init/build-context instrumentation can observe all required commands without a second application build or modification of immutable inputs.
- [x] Verify maximum provenance, actual compiler selection, build-context Node evidence, BuildKit identity and base materials can be retrieved and bound to every publication output. Version commands must belong to successful owned execution; report missing evidence as failure.
- [x] Save the probe commands, output paths and one capability verdict in this plan. If any required capability is missing, stop remaining tasks and report the exact plugin gap for a separately reviewed change. Do not add a prebuilt bypass or substitute requested versions.
- [x] Commit the capability record as `Record soak recipe observation capability`; include only this plan and compact textual evidence references, not generated build workspaces.

Expected result: all required capabilities demonstrated, with the concrete mechanism recorded; otherwise a bounded capability-gap report. A profile-only `validateRecipe` success does not pass this gate.

## Task 2: Add recipe selection and snapshot-preserving staging

**Files:** Create `tasks/soak/recipe.py`, the recipe/scenario named in the spec, and `tests/soak/test_recipe.py`; modify `config/scenario.py`, `tests/config/test_recipe.py`, `tests/soak/test_presets.py` and `.github/workflows/ci.yml`.

**Interfaces:** Add `validate_soak_recipe(profile: Path, config: SoakConfig, *, platform: str) -> None` and `prepare_soak_recipe_run(snapshot: SourceSnapshot, profile: Path, run_dir: Path, tag: str) -> RecipeRun`. Reuse the existing `RecipeRun` fields; stage snapshot contents rather than calling tracked-only `prepare_recipe_run` on the operator checkout.

- [x] Write failing tests `test_container_smoke_accepts_recipe_profile`, `test_soak_recipe_path_is_relative_to_scenario`, and parameterized `test_soak_recipe_rejects_unsupported_selection`: reject P24, native, containerd/k8s, conflicting image overrides, missing profile and build/prebuilt contradictions. Confirm `recipeProfile` is admitted in both the generic recipe guard and soak's explicit allowed-field set.
- [x] Write `test_soak_recipe_matches_all_role_expectations`: assert exact roles/modules, JVM/default variants, required provenance, one platform, no services, selected registry and matching `soak.images`. Reject every mismatch before resource acquisition; use daemon ARM64 versus declared AMD64 as a failure case.
- [x] Write `test_recipe_staging_preserves_dirty_and_untracked_snapshot` and `test_recipe_staging_rejects_mutation`: verify file bytes, modes, deletions and symlinks according to the existing inventory policy. Assert the original revision and snapshot fingerprint remain distinct from any synthetic staging Git identity.
- [x] Run the new tests and confirm relevant failures; implement the two interfaces using existing snapshot materialization/verification and strict profile parsing. Freeze/hash the profile before commands; retain original and effective input identity separately.
- [x] Add the three-role profile and ARM64 scenario with the original smoke's limits, phase durations, rates and criteria. Use recipe function `name: word-stats` with `sdk: java` and `sdk: javascript`, mapping to roles `word-stats-java` and `word-stats-javascript`. Set JavaScript variant to `default` in role expectations. Give the new profile a stable checked-in tag overridden by the run tag.
- [x] Validate the profile with `./gradlew validateRecipe -Precipe=<absolute-new-profile>` in the pinned checkout; add the same validation to CI. Run `uv run --locked --package nanolab pytest packages/nanolab/tests/config/test_recipe.py packages/nanolab/tests/soak/test_recipe.py packages/nanolab/tests/soak/test_presets.py -q`; expect all pass.
- [x] Commit only Task 2 files: `Add strict recipe inputs for container smoke soak`.

## Task 3: Verify attested single-platform publications

**Files:** Create `tasks/soak/recipe_registry.py` and `tests/soak/test_recipe_registry.py`; share existing `tasks/recipe_registry.py` byte-fetching helpers where compatible without changing its multiarch contract.

**Interfaces:** Define frozen `VerifiedSoakImage` with `publication_digest: str`, `manifest_digest: str`, `config_digest: str`, `provenance: tuple[dict[str, object], ...]`. Add `verify_soak_registry(distribution: MultiarchDistribution, *, platform: str, evidence_dir: Path, fetch: RegistryFetch, artifact_limit_bytes: int = 16 * 1024 * 1024) -> dict[str, VerifiedSoakImage]`, keyed by the existing soak role names.

- [x] Write `test_attested_publication_selects_only_executable_manifest`: assert selected host platform/config digest and attestation-to-manifest binding; retain index identity separately. Include direct executable manifests only when independently verifiable provenance still meets the required contract.
- [x] Write parameterized `test_invalid_publication_fails_before_runtime` covering wrong byte hash, duplicate JSON keys, descriptor size/type, platform/CPU variant, duplicate executable descriptors, missing/foreign attestation, wrong subject, changed tag and absent material evidence. Require rejection of missing/wrong maximum-provenance contents through existing collector checks.
- [x] Run the new test file and confirm failures. Implement content-byte verification for index, manifest, configuration and attestation blobs; persist raw verified objects and their identity map. Accept only local registry references; preserve existing timeout/no-proxy/no-redirect restrictions and evidence quotas.
- [x] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/soak/test_recipe_registry.py packages/nanolab/tests/tasks/test_recipe_registry.py -q`; expect all pass and the previous multiarch descriptor restrictions unchanged.
- [x] Commit Task 3 files: `Verify attested recipe images for soak`.

## Task 4: Publish once and preserve observed build receipts

**Files:** Extend `tasks/soak/recipe.py`; create `tasks/soak/recipe_observation.py` and `tests/soak/test_recipe_observation.py`; adapt shared helpers in `build_observation.py`/`build_provenance.py` only as required by the capability gate; extend `tests/soak/test_recipe.py`.

**Interfaces:** Add `publish_soak_recipe(snapshot: SourceSnapshot, profile: Path, config: SoakConfig, *, run_dir: Path, tag: str, builder: str, executor: OwnedBuildCommandExecutor, artifact_limit_bytes: int) -> tuple[BuildReceipt, ...]`. Its observation bridge exports `observe_soak_recipe(run: RecipeRun, snapshot: SourceSnapshot, *, executor: OwnedBuildCommandExecutor, builder: str, artifact_limit_bytes: int) -> tuple[MultiarchDistribution, dict[str, ObservedBuild]]`. The bridge owns exactly one publication and returns request-bound observed results by role; the publication adapter validates artifacts and freezes receipts.

- [x] Write `test_one_snapshot_one_publication_all_receipts`: instrument the owned executor, assert exactly one `publishRecipe`, no `assembleRecipe`/legacy build, the explicit builder and unique tag, and exactly three receipts sharing the snapshot fingerprint. Verify raw staged profile/report and hashed observation/log references are retained.
- [x] Write `test_requested_versions_cannot_satisfy_observation` and parameterized `test_recipe_observation_rejects_unbound_output`: wrong request/workspace/image/source binding, failed compilation, unrelated executable, stale logs, absent role, missing toolchain and mismatched base material must fail. Include successful real build-context evidence fixtures based on Task 1 rather than inventing a transport schema unrelated to the plugin.
- [x] Write `test_partial_publish_has_no_frozen_receipts`, `test_cancelled_recipe_reaps_owned_commands` and `test_quota_failure_preserves_original_error_and_cleanup`. Assert no deployment, no silent publication retry, retained bounded partial evidence and cleanup of owned subprocesses.
- [x] Run the tests to confirm failures. Implement the bridge using Task 1's demonstrated mechanism and existing owned execution. Record instrumentation hashes and original/effective input fingerprints; do not mutate the snapshot or operator checkout. Reuse current successful-command/material validation instead of weakening it or synthesizing effective versions.
- [x] Verify source/profile identity across publication, validate distribution, call Task 3 registry verification, and adapt results into existing receipts. Runtime references must use executable manifest digests; receipts/logs must bind effective build inputs to the original snapshot.
- [x] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/soak/test_recipe.py packages/nanolab/tests/soak/test_recipe_observation.py packages/nanolab/tests/soak/test_build_observation.py packages/nanolab/tests/soak/test_build_provenance.py packages/nanolab/tests/soak/test_build_executor.py -q`; expect all pass.
- [x] Commit Task 4 files: `Publish soak recipes with observed build receipts`.

## Task 5: Connect recipe preparation to the owned soak runtime

**Files:** Modify `tasks/soak/preparation.py`, `tasks/soak/runtime.py`, `plans/soak.py`; extend `tests/soak/test_preparation.py`, `tests/plans/test_soak.py`, `tests/soak/test_runtime.py`, `tests/soak/test_lifecycle.py` and `tests/cli/test_command_surface.py`.

**Interfaces:** Extend `PreparationOptions` with `recipe_profile: Path | None = None` and `recipe_builder: str | None = None`. Preserve `prepare_soak` and `PreparedSoak` public contracts. `RunSingleVersionSoak` passes `ScenarioConfig.recipe_profile` and the actual acquired builder to deferred preparation; it must reject conflicting injected options. Keep existing receipt consumers and non-recipe preparation behavior.

- [x] Write `test_recipe_selection_reaches_deferred_preparation`: compiling/inspecting the workflow invokes neither Docker nor source capture; incompatible profile/platform inputs fail before acquisition. The run passes the acquired builder identity, not an unrelated helper/default builder name.
- [x] Write `test_recipe_preparation_skips_legacy_builds`: assert `publish_soak_recipe` consumes the one captured snapshot and its receipt tuple enters `PreparedSoak`. Construct the existing internal `BuildRecipe` entries only as required by current consumers; never invent a bake descriptor as evidence of what the plugin executed.
- [x] Preserve the runtime report's role-to-file contract: write `recipe-{index}.json` and `builds/build-{index}.json` in `config.roles` order. Recipe-path descriptors use `bake=None` and `prerequisite_argv=None`; these are reporting descriptors, never passed to legacy `BuildImagesTask`. Add `test_recipe_report_references_existing_receipts` asserting every report reference resolves to the matching role's hashed evidence.
- [x] Write `test_frozen_recipe_runtime_checks_manifest_and_config`: frozen Compose/functions use verified executable digest references, pulled/deployed image IDs match verified config digests, and effective runtime checks remain mandatory. Assert no build/push during deployment or measurement.
- [x] Write failure/cancellation cases for distribution/receipt mismatch, evidence writes and partial acquisition: release only owned registry/builder/endpoint/runtime resources and preserve operator resources and the original selected builder. Assert restart/reentry retains the existing explicit rejection; keep/teardown follow current journal authority.
- [x] Run new cases to confirm failures. Wire the recipe branch at the existing source-acquisition boundary. Ensure preflight profile compatibility precedes builder/registry acquisition; daemon support checks precede publication. Configure only the owned builder for the local HTTP registry and pass it explicitly; no global builder selection.
- [x] Preserve existing helper/prerequisite preparation and measurement contracts. Reconcile `127.0.0.1` registry selection explicitly with `PreparationOptions.registry`; do not allow silent disagreement with the staged profile.
- [x] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/plans/test_soak.py packages/nanolab/tests/plans/test_soak_prebuilt_images.py packages/nanolab/tests/soak packages/nanolab/tests/cli/test_command_surface.py -q`; expect all pass, including original legacy soak and prebuilt rejection.
- [x] Commit Task 5 files: `Use recipe receipts in container smoke soak`.

## Task 6: Demonstrate the full smoke and document the supported boundary

**Files:** Update `docs/soak.md`, `packages/nanolab/README.md`, `docs/recipes-roadmap.md` and this plan. Retain bulky run evidence outside Git.

**Interfaces:** Existing `nanolab run`, run evidence and cleanup journal; no new command or runtime policy. Record actual counters/verdicts and input revision/fingerprint.

- [x] Run `uv run --locked --all-packages --all-groups ruff check packages/nanolab`, `uv run --locked --all-packages --all-groups ruff format --check packages/nanolab`, `uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab`, and `uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache`. Run `pre-commit run bandit-nanolab --all-files` for the existing security gate and configured excludes. Expect zero findings/errors/broken contracts.
- [x] Run the full NanoLab suite: `uv run --locked --package nanolab pytest packages/nanolab/tests -q`. Record exit code and test count. Fix relevant failures before continuing.
- [x] Capture existing Docker containers/builders and selected builder; verify pinned NanoFaaS checkout/revision. Establish a fresh evidence directory, separate from previous multiarch runs.
- [x] Execute `uv run --locked --package nanolab nanolab run packages/nanolab/scenarios-v2/memory-soak-smoke-recipe-container.yaml --run-dir <absolute-evidence-dir> --environment packages/nanolab/environments/local.yaml`. Use a previously unused run directory and retain full CLI logs and exit code.
- [x] Inspect evidence: one application publication, one snapshot, exact roles, maximum provenance and actual toolchain/material evidence, verified manifests/configs, digest-fixed deployment, effective runtime preflight, prerequisites, workload, every measurement phase, numerical/completeness verdicts, report, `p24_qualified: false`, cleanup. A zero process exit alone does not pass this step.
- [x] Compare resource state before/after. Verify only owned resources were removed, preexisting resources/selected builder remain and no owned command is still running. Exercise a focused real preparation failure if synthetic tests leave a concrete cleanup risk.
- [x] Run the existing `deployment-lifecycle-container.yaml` recipe regression with host Docker and retain its evidence. Do not launch P24 or unrelated backend/matrix campaigns.
- [x] Document the new entry point and observations. Preserve P24 limitations and record any readiness failures precisely. Mark only this subset complete on the roadmap if all required phases/evidence succeed; otherwise record the blocker and leave it open.
- [ ] Self-review implementation against the spec and this plan, then obtain one fresh whole-branch review using the preserved native execution workflow. Fix concrete findings and repeat only the affected verification before committing the final docs/evidence summary.
- [ ] Commit `Document verified recipe container smoke soak`; present the branch integration choice after fresh verification. Do not merge/push implementation without the user's integration instruction.

## Plan self-review

- Spec coverage: Task 1 gates required observations; Task 2 covers public input/source contracts; Task 3 verifies publication artifacts; Task 4 preserves receipts; Task 5 owns runtime/lifecycle integration; Task 6 proves and documents the complete smoke.
- Review Focus: each listed failure class has named tests in its owning task.
- Capability uncertainty is explicit and blocks implementation beyond Task 1; it is not permission to reduce the evidence contract or change the plugin implicitly.
- No test or profile validation claims operational readiness. Final documentation distinguishes numerical smoke results, evidence completeness and P24 qualification.

## Execution handoff

Review this plan before implementation. Preserve the previously selected native
execution approach: implement task by task in this session, then perform one
fresh whole-branch review. Start with Task 1 and stop on a demonstrated capability
gap rather than executing later tasks speculatively.

## Task 1 capability result — 30 September 2026

Gate passed. Two disposable publications used the same captured source snapshot
with three application roles. The first used the floating BuildKit image and
produced v0.33.1 provenance rejected by the existing collector. The second used
`moby/buildkit@sha256:1e110c71d389d6d24f67b9438e2f7b8da749a6ff407b22a1631e025c95599368`
(v0.27.1), and all three published predicates/material sets passed its checks.
Both Gradle publications exited zero; both released their builder and registry.

Mechanism demonstrated: `registry.platforms: [linux/arm64]`,
`registry.provenance: true`, explicit `recipeBuilder`, and supported `recipeDocker`
command interception to retain each temporary metadata file before the plugin
deletes it. A Gradle init script observes the selected JavaCompile compiler and
actual Gradle distribution; a workspace-only Node preload observes the executable
used by successful npm/tsc build stages. No plugin or operator-checkout edits.

The pinned probe recorded 24 successful toolchain observations, three output
metadata files bound to the published digests and actual builder/node, three
attested indexes and executable manifest mappings. Baseline/final containers
match, the selected `nanolab-heap-analysis` builder remains, and both probe
builders are gone. Full soak runtime execution is still pending.

Evidence: `/tmp/nanolab-soak-recipe-capability-20260930/pinned-builder/`, including
`verification.json`, `publication.log`, `docker-commands.jsonl`, metadata files,
per-component manifest/provenance/image output and resource state.
Original snapshot fingerprint:
`25eea60e44010ed346888db88c022a93864fa610f14a96cc8d059c5147377baf`; original
NanoFaaS revision `e7914be065e844776af57fe9e449bce7f12e03c5`, clean.

Ruling: pin the supported BuildKit image for this recipe path, preserving the
existing strict provenance checks; no fallback to the floating incompatible
format. Cost if wrong: an explicit builder-image update and renewed verification.

Ruling: reuse the published Buildx DTO (`MultiarchDistribution`) for the
single-platform attested path, introducing a parameterized `read_buildx_distribution`
reader while keeping `read_multiarch_distribution`'s existing two-platform,
provenance-disabled defaults unchanged. The plain `RecipeDistribution` reader
requires local IDs which Buildx does not provide. Cost if wrong: shared reader
regression, covered by the existing multiarch suite.

## Full smoke evidence

The final local ARM64 smoke (`run-6`) completed every phase and retained all
required evidence. One root publication, the single original source fingerprint
`25eea60e44010ed346888db88c022a93864fa610f14a96cc8d059c5147377baf`,
three executable/configuration identities, actual Java 25.0.4/Gradle 9.7.1/
Node 20.20.2/BuildKit v0.27.1, offline provenance and runtime checks passed.
Steady: 122 requests, zero HTTP errors/dropped iterations, 244/244 checks.
Report INCONCLUSIVE/exit 2 remains correct: three RSS growth criteria and
ownership/equal-work attribution are unresolved; all evidence gates pass.
Cgroup ceilings pass. No frozen criteria changed; p24_qualified remains false.

Resources after cleanup match the baseline containers, volumes and selected
builder. Ordinary JVM recipe regression completed 13 tasks/exit 0. Evidence
root: `/tmp/nanolab-soak-recipes-e2e-20260930/`, including `verification.json`.
Real attempts exposed and verified fixes for CLI recipe environment selection,
Node compiler observations during the plugin's cached push, quoted JVM launch
argfiles, recipe-specific offline acceptance, requested/effective fingerprint
reporting and bound empty diagnostic receipts. These preserve the spec's
evidence contract.

The full Bandit gate has four preexisting low-severity B101 assert findings;
new recipe/soak sources have no findings. Functional migration is complete;
RSS acceptance and the preexisting security-gate debt stay explicit.
