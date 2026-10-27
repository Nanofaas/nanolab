# Multiarch Recipes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Publish and verify JVM control-plane/word-stats images for AMD64 and ARM64, then validate their digest-pinned runtime on the Docker host.

**Architecture:** Keep a separate multiarch publication model and the strict existing local distribution reader. Verify immutable registry bytes, then project the host platform into the existing runtime distribution interface. Compose existing Sonata command/Docker tasks into owned builder/emulation resources and reuse the container lifecycle.

**Tech Stack:** Python 3.12+, Sonata engine/tasks 0.6.4, existing httpx, Docker Registry API v2, Buildx, QEMU/binfmt, Java/Gradle recipes v2.

**Spec:** [2026-09-30-multiarch-recipes-design.md](../specs/2026-09-30-multiarch-recipes-design.md)

## Global Constraints

- Platforms: exactly `linux/amd64` and `linux/arm64`; runtime invocation only on the Docker daemon's native platform.
- Components: JVM control plane and Java JVM `word-stats`; modules exactly `container-deployment-provider` and `build-metadata`.
- Registry: `127.0.0.1:5000/nanofaas`; `provenance: false`; existing run-specific recipe tag.
- Source: CI pin `e7914be065e844776af57fe9e449bce7f12e03c5`; capture revision, dirty state and tracked patch hash without editing that checkout.
- Host Docker only; dedicated `docker-container` builder; explicit `-PrecipeBuilder=<name>`; no global builder selection.
- Preserve existing builders, binfmt registrations, Minikube and unrelated containers. Compensate partial acquisitions and retain failure evidence.
- One `publishRecipe` invocation; no separate assembly or legacy build. Preserve the raw report; never synthesize its local image ID.
- Existing single-platform consumers stay strict. No native builds, services, attestations, signing, foreign-platform invocation, cron or resume semantics.
- Add no dependency or generic infrastructure task class; reuse Sonata command/Docker tasks. Run each task's red/green cycle and commit only its files.

## Review Focus

- Docker daemon architecture differs from the Python process architecture: select the daemon platform (Task 3).
- Registry redirects or nonlocal references could fetch evidence from an unintended endpoint: constrain transport to the selected local registry (Task 2).
- Two NanoLab runs share a foreign-architecture registration: hold the daemon-scoped lock for its full acquisition lifetime (Task 3).
- A previous failed run leaves a runtime mapping: remove it before publication and publish a new mapping only after complete verification (Task 4).
- Scenario planning, unsupported backend selection or resume could mutate the host prematurely: reject unsupported inputs and keep plan compilation side-effect free (Task 5).

## Files and boundaries

- `tasks/recipe.py`: existing local model; share profile identity validation and Gradle invocation/logging without loosening its reader.
- New `tasks/recipe_multiarch.py`: publication model/parser, verified host projection and publication resource.
- New `tasks/recipe_registry.py`: byte-preserving local registry transport and immutable artifact verification.
- New `tasks/recipe_builder.py`: recipe-specific owned builder/binfmt composition using existing infrastructure tasks.
- `plans/validate.py`: select this path only for the supported host container recipe.
- `comparison/evidence.py`: reuse only small digest/descriptor primitives if useful; preserve its single-platform contract.
- New tests mirror these boundaries; profiles/scenarios/CI/README/roadmap complete the integration.

Run commands below from the isolated NanoLab worktree. Set
`NANOFAAS_ROOT=/home/michele/Documenti/nanofaas/.worktrees/nanolab-merge-e7914be0`
in the execution environment. Use `uv run --locked --package nanolab pytest`
for tests; focused runs use `--no-cov`. Dependency setup belongs to execution,
not this documentation stage.

### Task 1: Multiarch publication contract

**Files:** Create `packages/nanolab/src/nanolab/tasks/recipe_multiarch.py` and `packages/nanolab/tests/tasks/test_recipe_multiarch.py`; modify `packages/nanolab/src/nanolab/tasks/recipe.py` and its existing tests.

**Interfaces:** Produce frozen `MultiarchImage(reference: str, status: str, digest: str, platforms: tuple[str, ...], manifests: dict[str, str], provenance: bool)`, `MultiarchComponent` with the existing component metadata fields and `image: MultiarchImage`, and `MultiarchDistribution` with the existing distribution metadata fields and multiarch components. Produce `read_multiarch_distribution(report: Path, *, recipe: Path, tag: str, expected_source: dict[str, object]) -> MultiarchDistribution`. Common profile identity checks accept either component representation; local `RecipeImage.id` remains required.

- [x] **Step 1: Add failing contract tests.** `test_read_multiarch_distribution_without_local_id` asserts exactly two platforms, index/child digests and source identity on both expected components. Parameterize `test_reject_inconsistent_multiarch_report` over recipe hash/tag/source, modules, kind/SDK/mode/variant/optimization, duplicate components/references, platform duplicates/missing/extra values, malformed digests, provenance and `published-unverified`. Use real SHA-256 fixtures, not shortened fake digests. `test_local_reader_still_rejects_multiarch` asserts the existing error.
- [x] **Step 2: Run** `uv run --locked --package nanolab pytest packages/nanolab/tests/tasks/test_recipe_multiarch.py packages/nanolab/tests/tasks/test_recipe.py --no-cov -q`; confirm new tests fail because the interface is absent.
- [x] **Step 3: Implement the model/parser and shared identity checks.** Reject duplicate JSON object keys as well as duplicate arrays; this prevents a repeated platform key from silently overwriting a manifest. Require schema version 2, exact recipe platform set, SHA-256 digests and matching captured source revision/dirty state. Keep patch verification in source staging. Share only validation that applies to both models; do not insert dummy image IDs or globally make IDs optional.
- [x] **Step 4: Run the same tests.** Require all new and existing local-reader tests to pass.
- [x] **Step 5: Commit** these contract/model changes with `Add strict multiarch recipe publication contract`.

### Task 2: Verify registry bytes and derive host identity

**Files:** Create `packages/nanolab/src/nanolab/tasks/recipe_registry.py` and `packages/nanolab/tests/tasks/test_recipe_registry.py`; extend `recipe_multiarch.py` and its tests. Modify `comparison/evidence.py` and `tests/comparison/test_evidence.py` only if extracting a genuinely shared primitive.

**Interfaces:** Produce `RegistryResponse(body: bytes, digest: str | None, media_type: str)` and `RegistryFetch = Callable[[str, str, str], RegistryResponse]`, whose arguments are repository path, object kind (`manifests`/`blobs`), and tag/digest. Produce `verify_multiarch_registry(distribution: MultiarchDistribution, *, fetch: RegistryFetch, evidence_dir: Path) -> dict[str, dict[str, str]]`, keyed by original image reference then platform, with configuration digests as values. Produce `project_host_distribution(distribution: MultiarchDistribution, *, platform: str, configs: dict[str, dict[str, str]]) -> RecipeDistribution`.

- [x] **Step 1: Add failing tests.** `test_verifies_both_platforms_and_retains_raw_bytes` builds a complete byte fixture index/manifests/configs and asserts all hash/link evidence. Parameterize `test_registry_identity_mismatch_stops_projection` over raw hash mismatch, wrong report/header digest, duplicate/extra/missing descriptors, attestation, wrong config platform, missing blob and changing tag. `test_registry_fetch_is_scoped_and_bounded` asserts nonlocal reference rejection, redirects disabled and explicit timeout. `test_host_projection_uses_child_digest_and_config_id` covers both host platforms and asserts unchanged raw publication plus `repo@child_digest`, expected config ID and complete component metadata.
- [x] **Step 2: Run** both new test files with `--no-cov -q`; require failures at the new verifier/projection interfaces.
- [x] **Step 3: Implement verifier, projection and HTTP transport.** Use existing httpx with redirects disabled, `trust_env=False`, a 30-second request timeout and the fixed local HTTP registry origin. Accept OCI/Docker v2 index and manifest media types, requesting them explicitly; validate schema and descriptor sizes/digests. Hash untouched response bytes, fetch config blobs immutably, and check Linux/architecture. Resolve the tag before and after verification, requiring the report index digest both times. Write per-component raw files and summaries under `evidence_dir`; return configuration digests only when every component succeeds. Projection selects a verified child manifest and fills existing `RecipeImage.id` from its config digest, retaining the child manifest as runtime digest. Keep comparison's single-executable-manifest rejection tested if sharing helpers.
- [x] **Step 4: Run** registry/model tests and `packages/nanolab/tests/comparison/test_evidence.py`; require all to pass.
- [x] **Step 5: Commit** with `Verify multiarch registry artifacts and host image identity`.

### Task 3: Owned Buildx and scoped QEMU resources

**Files:** Create `packages/nanolab/src/nanolab/tasks/recipe_builder.py` and `packages/nanolab/tests/tasks/test_recipe_builder.py`.

**Interfaces:** Produce `RecipeBuilder(name: str, platform: str)` and `recipe_builder_resource(*, executor: CommandTaskExecutor, run_dir: Path, tag: str, requires: tuple[Resource[Any], ...] = ()) -> Resource[RecipeBuilder]`. Consume the existing registry prerequisite; create commands lazily in resource acquisition. Use repository constants `BINFMT_INSTALLER_IMAGE` and `FOREIGN_PROBE_IMAGE`, each an immutable image reference.

- [x] **Step 1: Add failing executor/resource tests.** `test_builder_uses_daemon_architecture_and_explicit_name` asserts host network, scoped registry configuration, both advertised platforms and no `--use`. `test_existing_foreign_registration_is_preserved` asserts no installer/removal. `test_owned_registration_is_removed_only_when_unchanged` covers successful cleanup and externally modified entries. Parameterize `test_builder_compensates_partial_acquisition` over installer/probe/create/bootstrap failures and cancellation. `test_builder_name_collision_never_removes_existing_builder` proves cleanup ownership. `test_two_runs_serialize_registration_lifetime` proves the second cannot borrow a registration while the first owns its lock.
- [x] **Step 2: Run** `uv run --locked --package nanolab pytest packages/nanolab/tests/tasks/test_recipe_builder.py --no-cov -q`; confirm absent-resource failures.
- [x] **Step 3: Resolve immutable tool images.** Inspect `tonistiigi/binfmt` and a minimal `/bin/true`-capable multiarch probe image through the registry, confirm AMD64 and ARM64 coverage, then store their observed index digests as constants. Record these references in evidence. No privileged installer is executed in this step.
- [x] **Step 4: Implement the resource.** Read Docker daemon OS/architecture, inspect binfmt and active builder, acquire a daemon-scoped host lock before registration inspection and hold it until release/compensation. Use safe lock-file creation and reject symlinks; coordinate by daemon identity, not run directory. Install only `amd64` on ARM64 or `arm64` on AMD64 when absent; reject preexisting disabled/incompatible entries. Probe the foreign platform using the pinned image. Create a unique builder without selection, bootstrap and require both platforms. Inspect binfmt through a narrowly scoped privileged helper when host permissions require it; remove an owned entry only after comparing current registration with recorded state. Retain cleanup conflicts and primary failures separately. Compensate immediately inside failed acquisition because the engine cannot release a value it never received.
- [x] **Step 5: Run** builder tests; require full ownership/failure matrix to pass and no real Docker mutations in unit tests.
- [x] **Step 6: Commit** with `Manage owned multiarch recipe builder and emulation`.

### Task 4: Publish once and expose verified runtime inputs

**Files:** Modify `packages/nanolab/src/nanolab/tasks/recipe.py` and `packages/nanolab/src/nanolab/tasks/recipe_multiarch.py`; extend `packages/nanolab/tests/tasks/test_recipe.py` and `packages/nanolab/tests/tasks/test_recipe_multiarch.py`.

**Interfaces:** Add optional `builder: str | None = None` to `recipe_command`. Extract `execute_recipe(run: RecipeRun, *, target: Literal['assembleRecipe', 'publishRecipe'], executor: CommandTaskExecutor, inputs: TaskInputs, builder: str | None = None) -> Path` for existing command/log/report handling. Produce `multiarch_recipe_distribution_resource(*, source: Path, recipe: Path, run_dir: Path, tag: str, executor: CommandTaskExecutor, functions: tuple[tuple[str, str], ...], builder: Resource[RecipeBuilder]) -> Resource[RecipeDistribution]`.

- [x] **Step 1: Add failing tests.** `test_builder_property_is_forwarded_only_when_selected` asserts exact `-PrecipeBuilder=<name>` and unchanged existing commands. `test_multiarch_publication_runs_once_before_projection` asserts staged source/hash identity, one publish command, external Gradle log, registry verification before runtime mapping and exact selected modules/functions. `test_failed_publication_invalidates_previous_runtime_mapping` asserts no stale usable mapping on command/parser/registry failures, preserving diagnostics. `test_multiarch_source_change_fails_before_publish` proves staged inputs remain checked.
- [x] **Step 2: Run** recipe and multiarch tests with `--no-cov -q`; require new behavior to fail.
- [x] **Step 3: Implement the interfaces.** Treat `run_dir` as the scenario evidence root; stage with `prepare_recipe_run(source, recipe, run_dir / 'recipe', tag)` and place registry bytes under `run_dir / 'registry'`. Record expected revision/dirty identity from captured inputs and verify staged bytes before publication. Reuse `execute_recipe` in existing recipe tasks and the multiarch resource. Remove any prior `runtime-images.json` before attempting publication. Parse raw multiarch evidence, enforce exactly the supported two JVM components/modules, verify all artifacts with Task 2, then derive the host view. Write the complete runtime mapping atomically beside the run's evidence only after success. Keep the original index/platform evidence and raw report intact; return the projected distribution for existing lifecycle consumers.
- [x] **Step 4: Run** recipe/model/registry/builder tests plus remote recipe, Kubernetes and containerd recipe tests; require unchanged command paths and strict consumers to pass.
- [x] **Step 5: Commit** with `Publish multiarch recipes with verified host runtime inputs`.

### Task 5: Integrate the container scenario and CI profile

**Files:** Modify `packages/nanolab/src/nanolab/plans/validate.py`, `packages/nanolab/src/nanolab/cli/product.py`, `packages/nanolab/tests/plans/test_recipe_validate.py`, `packages/nanolab/tests/cli/test_command_surface.py`, `.github/workflows/ci.yml`; create `packages/nanolab/recipes/validate-container-multiarch-jvm.yaml`, `packages/nanolab/scenarios-v2/deployment-lifecycle-container-multiarch.yaml`.

**Interfaces:** Consume Task 3/4 resource factories in the existing container branch. Keep `RecipeBinding.distribution: Resource[RecipeDistribution]` unchanged. Select the path from captured recipe `registry.platforms`, with no new scenario field. Reject multiarch for other backends/environment providers and reject resume before resource acquisition; preserve side-effect-free planning.

- [x] **Step 1: Add failing plan/profile tests.** `test_multiarch_plan_reuses_container_lifecycle_without_builds` asserts registry → builder → publication → Compose dependencies, existing metadata/image/invocation/resource tasks and no legacy builds. `test_multiarch_planning_has_no_host_side_effects` asserts no commands or run-directory creation during compilation. Parameterize `test_unsupported_multiarch_workflow_fails_before_resources` over Kubernetes/containerd, Multipass, unsupported components/provenance/platform set and resume. `test_multiarch_profile_matches_existing_jvm_contract` asserts exact modules/function, JVM args/backend config, two platforms and disabled provenance. Retain existing single-platform plan tests.
- [x] **Step 2: Run** plan/config tests with `--no-cov -q`; require new selection/integration tests to fail.
- [x] **Step 3: Implement dispatch and checked-in inputs.** Copy the existing JVM profile's build/JVM/runtime settings, set a distinct multiarch profile name/variant/tag and add both platforms plus `provenance: false`. Copy the current JVM container scenario selecting this profile. Compose the new resource path into existing `RecipeBinding`, Compose and function registration. Add this profile to current CI `validateRecipe` invocations. Verify resource teardown order closes runtime dependencies before publication prerequisites. Preserve existing CLI keep/teardown policy and ownership records; do not offer recipe rebuild as resume recovery.
- [x] **Step 4: Run** focused plans/config/lifecycle tests. Run `validateRecipe` for the new profile using the pinned checkout: `./gradlew validateRecipe -Precipe=/tmp/nanolab-recipe-multiarch/packages/nanolab/recipes/validate-container-multiarch-jvm.yaml`. Require exit 0; the source checkout remains unchanged.
- [x] **Step 5: Commit** with `Add multiarch recipe container validation scenario`.

### Task 6: Verify actual publication, runtime and cleanup; document evidence

**Files:** Modify `packages/nanolab/README.md` and `docs/recipes-roadmap.md`; fix only concrete failures found by these checks in their owning files/tests.

**Interfaces:** Consume the new scenario and run artifacts from Tasks 1–5. Produce documented actual results and branch review evidence; publication-only success cannot mark the roadmap complete.

- [x] **Step 1: Run the full suite.** `uv run --locked --package nanolab pytest packages/nanolab/tests -q`; require all tests to pass. Run repository-required lint/type gates for the changed modules. Resolve concrete failures before Docker E2E.
- [x] **Step 2: Capture the host baseline.** Record `docker ps`, `docker buildx ls`, selected builder and relevant binfmt state. Check the pinned NanoFaaS revision and clean working tree. Record baseline under `/tmp/nanolab-multiarch-recipe-e2e-20260930/`; use a fresh run directory if it already exists.
- [x] **Step 3: Run the actual scenario.** `./nanolab.sh run packages/nanolab/scenarios-v2/deployment-lifecycle-container-multiarch.yaml --run-dir /tmp/nanolab-multiarch-recipe-e2e-20260930/run`. Require exit 0, one recipe publication, two independently verified artifacts per component, host child-digest references, exact running config IDs, matching metadata, successful word-stats assertions and successful owned cleanup. Keep the full log and registry bytes. This step includes the approved scoped privileged QEMU setup when needed.
- [x] **Step 4: Run the direct regression.** `./nanolab.sh run packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml --run-dir /tmp/nanolab-multiarch-recipe-e2e-20260930/regression-jvm`. Require exit 0. Compare host state with Step 2: selected/previous builders, preexisting binfmt, Minikube and unrelated containers preserved; run-owned builders/containers/registrations released. Do not broaden to unrelated VM workflows unless a concrete regression requires it.
- [x] **Step 5: Document verified behavior.** Add README usage, Docker/Gradle and temporary privileged emulation prerequisites, host-only runtime coverage, evidence format and cleanup behavior. Update the multiarch roadmap item with actual source revision/date/evidence and explicitly retain native/full-matrix work as open. No completion claim if E2E or cleanup fails.
- [x] **Step 6: Review and commit.** Run `git diff --check`, inspect changed files and obtain the whole-branch review required by the selected execution workflow. Fix actionable findings and rerun only affected gates. Commit with `Document verified multiarch recipe lifecycle`. Preserve unrelated primary-checkout edits; integration and push follow the user's chosen finishing action.

## Execution handoff

Recommended: native execution in this session. The six tasks depend closely
on the publication/verification/resource interfaces, and the existing
container lifecycle can be reused. Run a fresh whole-branch review after
implementation and before integration. Written-plan approval is required
before starting; the previously requested native method remains the default
unless the user changes it.

## Execution results

- Full suite: 3052 passed; lint, type, Bandit and import contracts passed.
- Final Docker/Gradle E2E: 15 tasks passed, NanoFaaS `e7914be0`, ARM64 host runtime and both platform artifacts verified.
- Direct JVM regression: 13 tasks passed. Original containers and selected builder preserved; owned QEMU/builder released; NanoFaaS unchanged.
- Evidence: `/tmp/nanolab-multiarch-recipe-e2e-20260930/run-3/`, `e2e-3.log` and `regression-jvm.log` beside it.
- Fresh whole-branch review: four Important findings and one Minor regraded Important; all five fixed with failing regression tests followed by green focused/full suites. No deferred findings.

## Recorded execution decisions

- Ruling: Test commands use the worktree .venv directly when sandbox blocks uv cache writes — same locked installed environment — no behavior change; costs stale environment if dependencies change (none added).
- Task 3: Ruling: fail fast when the daemon-scoped binfmt lock is busy instead of waiting — avoids indefinitely blocked acquisitions while preserving lifetime serialization — cost: simultaneous runs must retry.
- Task 5: Ruling: reject --keep/--teardown for this scenario — existing generic persistence cannot preserve an open host lock or rehydrate cleanup closure ownership across processes; automatic release meets the temporary-emulation spec — cost: multiarch deployments cannot be retained until durable ownership is designed.
- Task 6: Ruling: use standard textual Buildx inspect output instead of --format — installed Buildx rejects --format and Gradle also consumes the standard Platforms lines — cost: parser depends on the stable Platforms label. Reproduced RED with actual-format fixture before correction.
- Task 6: Ruling: pin the helper/probe to platform-specific manifests from the recorded immutable Busybox index — fixes Docker's cannot-overwrite-digest failure without deleting existing images — cost: pinned tool constants contain both platform child digests.
- Final: Ruling: lock directory is fixed /tmp, independent of TMPDIR — daemon-global ownership needs a shared location on this Linux host — cost: custom temporary storage cannot relocate this lock.
- Final: Ruling: builder node uses a unique ownership nonce and cleanup reconciles it even after interrupted create — sufficient evidence without deleting another builder of the same name — cost: one extra builder inspection during cleanup.
- Final: Ruling: baseline CPU aliases are AMD64 absent/empty/v1 and ARM64 absent/empty/v8 — reject higher or incompatible variants in descriptors and configs — cost: higher ISA builds require a separately declared platform contract.
- Final: Ruling: native builds/component matrices/foreign lifecycle execution remain excluded — JVM publication and host runtime are the approved slice — cost: no proof for those additional cases.
- Final: Ruling: signing/attestations/tool-origin trust beyond digest pinning remain excluded — no provenance in this profile — cost: no authenticity or provenance claim.
- Final: Ruling: filesystem-layer verification and foreign binary semantic correctness remain excluded — accepted identity contract covers index/manifest/configuration — cost: foreign runtime behavior remains unproven.
- Final: Ruling: SIGKILL/host-loss/restart recovery remains excluded — durable ownership and resume are not implemented — cost: abrupt termination can require manual cleanup.
- Final: Ruling: remote Docker/client-machine coordination remains excluded — this workflow requires host Docker and host-local locking — cost: not suitable for shared remote daemons.
- Final: Ruling: inherited registry/Compose naming is unchanged — no new regression established — cost: inherited naming limits persist.
- Final: Ruling: busy-lock fail-fast and keep/teardown rejection stand — recorded choices preserve temporary resource ownership — cost: users retry concurrent runs and cannot retain this deployment.
- Final: Ruling: direct JVM regression and roadmap completion are now accepted — regression-jvm exit0/all13tasks passed after the review snapshot — cost: no additional backend regression claim.
