# ARM64 Release Recipes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ARM64 release Bake with reusable recipes and separate assembly/push receipts while sharing the verified AMD64 execution path.

**Architecture:** Freeze both architecture matrices and six profiles before provisioning. Parameterize the existing release recipe helpers by architecture/role, route local-image verification to the corresponding daemon, and insert an ARM64 staging-push phase before smoke. Preserve all canonical source, benchmark/regression and publication/signing barriers.

**Tech Stack:** Python 3.12+, NanoLab/uv, Sonata workflows/resources, Gradle recipes v2, Docker/Buildx/BuildKit, Skopeo, native Azure ARM64 VM.

**Spec:** `docs/superpowers/specs/2026-10-02-release-arm64-recipes-design.md` (approved).

## Global Constraints

- NanoLab implementation base `46894690246a1e059913151b97e2b649855e733b`; NanoFaaS pin `e7914be065e844776af57fe9e449bce7f12e03c5`, version `0.22.0`. No pin/plugin change is assumed.
- Reuse the existing release-specific runner and phase/resource primitives; no second recipe runner, new framework or generic publish-only task.
- Supported pairs are AMD64/`stack` and ARM64/`arm-builder`. Native Linux host/daemon checks, explicit owned builder and independent image platform/ID checks are mandatory. Emulation does not qualify.
- Profiles: JVM/native/default counts 9/12/23, 44 per architecture at the pin; derive coverage from the guarded archive. Repository `127.0.0.1:5000/nanofaas`; suffixes `-arm64-jvm`, `-arm64-native`, `-arm64`. No recipe platforms/provenance.
- Eight modules: async-queue, autoscaler, build-metadata, concurrency-control, k8s-deployment-provider, offload, runtime-config, sync-queue. Preserve resolved `all` selection and reject drift before provisioning.
- JVM G1/C2 (`jvm-g1-c2`); Spring native Oracle/O3/G1/effective JFR (`native-o3-g1`); Java-lite Community/O3/serial. Default control plane is the exact artifact-only `image: null` component.
- Same original commit/archive/inventory on both VMs; preserve ignored-but-committed inputs, permissions and symlink checks. Python tests retain their external workspace and root pytest.ini.
- Keep source tests -> AMD64 assembly/push -> three benchmarks/aggregate/regression -> ARM64 assembly/push/smoke -> publication/manifests/aliases -> signing/finalization. No fake prerequisite receipts or threshold relaxation.
- `arm64-build` becomes local-ID evidence; `arm64-local-registry-push` provides registry digests. Never convert legacy combined receipts or interchange config IDs and manifest digests.
- Independent architecture input/output/log/evidence paths. Logs outside producer output. Partial/failed attempts issue no complete receipt; cleanup preserves diagnostics and unrelated resources.
- Outstanding AMD64 VM qualification remains open. ARM VM capability/runtime/resume and canonical-DAG checks are mandatory completion gates; unavailable access may defer them but not mark them passed.
- No public push/signing during these verification runs. Smoke limits, P24 and unrelated backend migrations remain deferred.

## Review Focus

1. A correct ARM tag exists only on the ARM daemon: resume must verify there, even if stack has the same logical target or a misleading tag (Task 3).
2. Transient input reacquisition encounters missing retained ARM evidence: do not repair it before resume verification or overwrite AMD64 evidence (Tasks 2/5).
3. A later recipe group replaces an earlier tag, or a tag changes between assembly and push: no stale build/push receipt may authorize smoke (Tasks 2/4).
4. A legacy combined `arm64-build` journal or stale smoke record survives upgrade: no reinterpretation as recipe/local-ID evidence and no publication bypass (Tasks 4/5).
5. Cancellation/transfer/cleanup failure occurs after one ARM group: retain diagnostics, remove owned ARM resources only and preserve AMD64 state and previous builder selection (Task 2).

## Working context and file responsibilities

Use `/tmp/nanolab-release-arm64-recipes`, branch `feature/recipe-release-arm64`.
Preserve unrelated dirty files in the primary NanoLab checkout. The existing
native execution preference persists; this plan still requires review before
execution.

Run test commands from this worktree with
`NANOFAAS_ROOT=/tmp/nanofaas-release-arm64-pin`.
Use `uv run --locked --package nanolab`. Baseline: 3,265 tests passed on integrated
main in 195.74s; rerun the baseline before product changes. Retain long outputs
in this plan's ignored execution workspace. No NanoFaaS edits; graph-first
exploration applies when investigating named NanoFaaS symbols under its AGENTS.

Responsibilities: `release/recipe.py` owns frozen profiles/report adaptation;
`recipe_execution.py` owns commands/source/transport/image proof; `resources.py`
owns staging/builder lifecycle; `evidence.py` owns fail-closed verification;
`plans/release.py` owns frozen request/provider/DAG binding; `release_phases.py`
owns assembly/push/smoke/publication receipt wiring; `tasks.py` owns phase
receipts; `arm.py`/`build.py` retain ARM smoke but lose unused release Bake work.

## Task 1: Freeze ARM64 profiles and both matrices before provisioning

**Files:** Create `packages/nanolab/recipes/release-arm64-{jvm,native,default}.yaml`; modify `release/recipe.py`, `plans/release.py`, `.github/workflows/ci.yml`; test `tests/release/test_recipe.py`, `tests/plans/test_release.py`, `tests/cli/test_release_command.py` (source/test paths below are relative to `packages/nanolab/src/nanolab` and `packages/nanolab`).

**Interfaces:** Extend `prepare_release_recipe_groups(source_tree: Path, image_plan: ImagePlan, *, profiles_root: Path, architecture: ImageArchitecture = "amd64") -> tuple[ReleaseRecipeGroup, ...]`. Keep `ReleaseRecipeGroup` fields unchanged (cells already carry architecture). Add `ReleaseRequest.arm_image_plan: ImagePlan | None = None` and `arm_recipe_groups: tuple[ReleaseRecipeGroup, ...] = ()`; executable request construction always fills both alongside existing AMD64 fields. Both plans come from the same extracted archive, never the live checkout.

- [x] Write `test_release_profiles_cover_both_architectures` with independently derived `ImagePlan`s: `assert counts == (9, 12, 23)` and `assert total == 44` for each architecture; exact tags/names/modules/native policies, default artifact-only CP and disjoint 88 references. Parameterize existing profile-drift tests for both architectures. Add `test_arm_profile_drift_fails_before_acquisition` and `test_arm_profile_freeze_uses_raw_bytes`: mutation after capture cannot change frozen bytes/hash/tag; a subsequent request sees the change.
- [x] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/release/test_recipe.py packages/nanolab/tests/plans/test_release.py packages/nanolab/tests/cli/test_release_command.py -q --no-cov`. Expected: new architecture arguments/request fields or ARM profile/preflight assertions FAIL; existing baseline passes.
- [x] Implement the interface, three ARM profiles and preflight request fields. Reject mixed cells, wrong architecture/registry, duplicate or missing cells and altered policy before provider creation/acquisition. Validate the frozen ARM fields in direct workflow requests; update existing fixtures with genuine prepared groups. Add three ARM `validateRecipe` commands to the existing CI gate; preserve the three AMD commands.
- [x] Validate each of the six checked-in profiles in a fresh materialized pinned archive with `./gradlew validateRecipe -Precipe=<absolute-profile> --console=plain`. Expected: all six PASS; retain logs and confirm resolved eight modules. No native exporter support is claimed by this check.
- [x] Rerun the focused command above. Expected: all PASS; commit `Add frozen ARM64 release recipe profiles`.

## Task 2: Share architecture-aware recipe execution and ownership

**Files:** Modify `release/recipe_execution.py`, `release/resources.py`; tests `tests/release/test_recipe_execution.py`, `tests/release/test_resources.py`, `tests/release/test_recipe.py`.

**Interfaces:** Add `architecture: ImageArchitecture = "amd64"` and `role: ExecutionRole = "stack"` keyword parameters to `release_recipe_commands` and `run_release_recipe_steps`, preserving all existing parameters/returns. Add `architecture: ImageArchitecture = "amd64"` to `release_recipe_inputs_resource` and `role: ExecutionRole = "stack"` to `preserve_release_builder_selection`. Reject invalid architecture/role pairs and mixed groups before remote work. The strict report adapter remains shared and unchanged unless an actual architecture-independent defect is demonstrated.

- [x] Parameterize the real-filesystem transport/external-producer fixture for both architectures. Write `test_recipe_commands_bind_architecture_role_and_paths`: `assert roles == {"arm-builder"}` for ARM; exact `release.arm64.recipe.<flavor>` IDs, owned ARM input/output paths, explicit builder env and tag overrides. Add `test_recipe_wrong_architecture_or_role_fails_before_remote_work`, `test_arm_recipe_reports_match_independent_daemon` and alias cases for host `aarch64`/`arm64`; reject wrong Linux OS/daemon/image architecture or ID.
- [x] Add architecture cases to source/symlink/ignored-addition, real-shell log ownership, stale-output failure, transfer truncation, cancellation and selected-builder tests. Add `test_arm_cleanup_preserves_amd64_inputs_and_diagnostics`, `test_arm_reacquisition_does_not_repair_retained_evidence`, `test_arm_final_union_detects_earlier_tag_replacement`. Assert 44 local-ID entries, three report/log entries, no receipt on any incomplete group and no shared-path mutation.
- [x] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/release/test_recipe_execution.py packages/nanolab/tests/release/test_resources.py packages/nanolab/tests/release/test_recipe.py -q --no-cov`. Expected: new ARM cases FAIL due to hard-coded AMD paths/role/host checks.
- [x] Implement architecture paths/config names, normalized native host/daemon checks and per-group/final-union image checks in the existing runner. Preserve hash/size transfer limits, archive/inventory checks, external logs and transient/retained separation. Bind builder inspect/restore to the requested role and compensate only owned paths. Keep default AMD behavior tested.
- [x] Rerun the focused command. Expected: both architecture suites PASS; commit `Share guarded release recipe execution across architectures`.

## Task 3: Route resume verification by the frozen image matrix

**Files:** Modify `release/evidence.py`, `plans/release.py`; tests `tests/release/test_evidence.py`, `tests/plans/test_release.py`.

**Interfaces:** Extend `release_evidence_verifiers(provider: object, request: object, *, ghcr_authfile: str | None = None, local_image_requests: Mapping[str, object] | None = None) -> dict[str, Verifier]`. The mapping contains complete `docker-daemon:` references and the corresponding VM request; when supplied it is authoritative and unknown references fail without probing. Preserve the standalone caller's existing stack-only behavior when omitted. `release_verifiers` constructs a complete map from both frozen matrices, rejects missing/ambiguous matrices and routes staging registry/GHCR lookups through the stack request.

- [x] Write `test_local_image_verifier_uses_exact_frozen_daemon_mapping`: mock two distinct daemon stores for identical logical targets. `assert arm_verified is True` when ARM image is absent from stack but present on ARM; wrong stack tag cannot satisfy ARM evidence. Assert exactly one selected VM call and no cross-daemon fallback. Add unknown reference, conflicting mapping and unavailable ARM VM cases: `assert verifier(evidence) is False`; never build/push while checking.
- [x] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/release/test_evidence.py packages/nanolab/tests/plans/test_release.py -q --no-cov`. Expected: mapping interface/routing tests FAIL, baseline verifier tests remain valid.
- [x] Implement exact-reference routing and plan map construction; use existing `_remote_image_digest` and existing evidence kinds. Confirm `local-registry-digest` always reads the stack registry and GHCR/signing behavior remains unchanged. Reject unknown/ambiguous references before inspection; no tag-substring heuristic or host-context fallback.
- [x] Rerun the focused command. Expected: PASS; commit `Verify release image evidence on its owning VM`.

## Task 4: Define shared assembly and separate ARM push phases

**Files:** Modify `plans/release_phases.py`, `plans/release.py` (AMD caller signatures only), `release/tasks.py`; test `tests/release/test_tasks.py`, `tests/plans/test_release.py`, `tests/tasks/migrated/test_release_composites.py`.

**Interfaces:** Replace the one-caller AMD phase constructor with `build_recipe_assembly_phase(*, architecture: ImageArchitecture, role: ExecutionRole, identity: ReleaseIdentity, run_dir: Path, image_plan: ImagePlan, max_parallelism: int, builder_name: str, remote_root: str, source_dir: str, executor: RoleBoundCommandTaskExecutor, prerequisite_phases: tuple[ReleasePhaseTask, ...], recipe_groups: tuple[ReleaseRecipeGroup, ...], provider: object, request: object, inventory_file: Path, archive_digest: str) -> tuple[tuple[str, ...], ReleasePhaseTask]`. Select existing AMD/ARM assembly factories; bind `recipeContract: 2` plus all Task 2 input identities in fingerprints. Do not keep an unused compatibility wrapper.

Extend `build_registry_push_phase` with explicit `architecture`/`role`, replace `amd64_build` with `assembly: ReleasePhaseTask` and `source_tests` with `prerequisite_phases: tuple[ReleasePhaseTask, ...]`; preserve remaining parameters/return. Add `arm64_registry_push_task(**kwargs: Any) -> ReleasePhaseTask` with phase `arm64-local-registry-push`, title `Push ARM64 images to local registry`. Add `role: ExecutionRole = "stack"` to `run_image_steps`, preserving its parameters/return. Existing ARM smoke/publication receipt readers remain unchanged until the atomic Task 5 DAG transition.

- [ ] Write `test_arm_assembly_and_push_have_distinct_receipts`: assembly contains 44 `local-image-digest` entries only; push contains 44 `local-registry-digest` entries only. `assert phase == "arm64-local-registry-push"` and `assert title == "Push ARM64 images to local registry"`. Add `test_changed_local_image_blocks_push_before_first_command`, `test_old_combined_arm_receipt_is_not_local_assembly_proof` and `test_arm_push_rejects_partial_or_foreign_digest_set`; inspect new receipts with existing `exact_receipt_artifacts`, requiring the new phase/kind. Partial/foreign digest sets fail.
- [ ] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/release/test_tasks.py packages/nanolab/tests/plans/test_release.py packages/nanolab/tests/tasks/migrated/test_release_composites.py -q --no-cov`. Expected: new phase/reader/pre-push identity tests FAIL.
- [ ] Implement shared assembly and push constructors. Before any push, read the exact local-ID assembly receipt and inspect on its owning daemon; compare all 44 IDs/platforms and abort on replacement. Reuse `registry_push_composite` on the selected role, then verify the complete registry set on stack. Use `run_image_steps` with an empty verification Steps for pre-push inspection (no build commands); keep actual push work separate. Fingerprint commands/role/images/TLS and prerequisite hashes.
- [ ] Update assembly factory descriptions and existing AMD caller imports/keyword arguments in the same commit. Keep the active legacy ARM DAG/readers intact until Task 5. Tests prove old combined receipts cannot satisfy the new local-ID/phase contract; Task 5 switches every ARM consumer atomically.
- [ ] Rerun the focused command with shared-constructor unit tests and updated fixtures. Expected: PASS; commit `Separate ARM64 assembly and staging push receipts`.

## Task 5: Wire the symmetric DAG, preserve resume and remove unused release Bake

**Files:** Modify `plans/release.py`, `plans/release_phases.py`, `release/tasks.py`, `release/arm.py`, `release/build.py`; tests `tests/plans/test_release.py`, `tests/cli/test_release_command.py`, `tests/release/test_arm.py`, `tests/release/test_build.py`, `tests/release/test_tasks.py`, `tests/release/test_recipe_execution.py`.

**Interfaces:** `build_arm64_phase` retains its existing inputs and adds `executor: RoleBoundCommandTaskExecutor`; consumes the frozen ARM plan/groups/inventory via request and returns `(arm_runtime_plan, arm_images, arm64_build, arm64_push, arm64_smoke)` instead of four items. Keep the existing runtime-plan model for smoke compatibility; do not generate Bake files merely to fill its legacy path fields. `build_publication_phase` accepts `arm64_push` instead of the combined `arm64_build` as registry prerequisite/source. Extend `registry_artifacts_from_receipt(receipt: Path, images: tuple[str, ...], *, phase: str = "arm64-local-registry-push") -> tuple[ArtifactEvidence, ...]`; replace `require_release_barriers.arm_build_receipt` with `arm_push_receipt` and read the new push phase strictly. Use Task 4 constructors for both architectures and Task 3 verifiers.

- [ ] Add `test_release_dag_has_symmetric_recipe_build_and_push`: exact phase order from the spec, two independent recipe-input/builder resources, ARM builder default loading, ARM push/smoke tunnel lifetime and preserved source staging. Assert no release Bake or direct bootJar/native preparation commands. Extend `test_release_recipe_prefix_until_staging_push_compiles_without_cloud` for `build-arm64-images`, `push-arm64-images-to-local-registry`, `test-arm64-images`: respectively stop before push, before smoke, before public publication/signing.
- [ ] Add `test_smoke_and_publication_require_exact_arm_push_receipt` to task tests: reject old combined receipt, partial/foreign registry matrix and a smoke file naming different digests even when regression passed. Extend the existing real Sonata workflow resume fixture with two VM stores. Write `test_both_architecture_resume_performs_no_build_or_push`, `test_arm_evidence_invalidation_preserves_verified_amd64_phase`, `test_legacy_arm_journal_requires_recipe_assembly_and_new_push`, `test_stale_arm_smoke_cannot_publish`. Parameterize deleted/tampered report/profile/config/inventory/receipt and replaced image cases; assert exact affected assembly/push/smoke/publication counts, prior receipt invalidation on failed rerun, no verification-time rebuilding and retained files not repaired by acquire.
- [ ] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/plans/test_release.py packages/nanolab/tests/cli/test_release_command.py packages/nanolab/tests/release/test_arm.py packages/nanolab/tests/release/test_build.py packages/nanolab/tests/release/test_tasks.py packages/nanolab/tests/release/test_recipe_execution.py -q --no-cov`. Expected: DAG/selection/legacy-resume cases FAIL on the old combined ARM path.
- [ ] Wire frozen ARM inputs, selected-builder restoration and default loading to `arm-builder`; assembly needs source/regression, push needs its completed assembly and tunnel, smoke needs push and tunnel. Route final registry inspection to stack. Connect smoke/publication readers to the new push receipt; preserve public matrix, benchmarks and signing behavior. Update direct request/test fixtures using Task 1 interfaces.
- [ ] Check all callers of `arm64_build_commands`, `_build_arm64_images` and release `build_inputs_resource` before deleting unused release Bake generation/staging. Keep ARM smoke helpers and other workflows' Bake functionality; replace obsolete tests with recipe/DAG assertions. Expected: text search shows no removed production symbol callers and no release Bake inputs.
- [ ] Rerun the focused command. Expected: PASS; commit `Use symmetric recipe phases in the release workflow`.

## Task 6: Verify native capability, real slice and canonical gates; document limits

**Files:** Update `README.md`, `packages/nanolab/recipes/README.md`, `docs/recipes-roadmap.md`, this plan; create retained private verification artifacts only under `/tmp/nanolab-release-arm64-verification/` (never commit secrets/log outputs).

**Interfaces:** Consumes the Task 1–5 executable DAG and unchanged VM/credential contracts. Produces explicit local/capability/slice/canonical PASS or incomplete records, without public publication/signing. Local implementation may proceed after Task 1 profile/catalog validation; no VM gate is marked complete by fixtures or host probes.

- [ ] Run `uv run --locked --package nanolab pytest packages/nanolab/tests -q` and `uv run --locked --package nanolab nanolab-quality`. Expected: complete suite PASS and `Quality checks passed`. Run `uv run --locked --package nanolab ruff format --check --config packages/nanolab/pyproject.toml packages/nanolab`; expected all formatted. Run `uv run --locked --package nanolab bandit -r packages/nanolab/src/nanolab -c packages/nanolab/pyproject.toml -f json`; expected only the four baseline B101 LOWs, retained nonzero status and no new findings. Run `git diff --check`; expected clean.
- [ ] Prepare private Azure environment/release credential config under the verification root from existing examples; validate clean pinned source, permissions and executable preflight without logging secrets. Confirm plan-mode `--until` contracts from Task 5 before cloud work. Expected: exact 88-cell request, six frozen profiles and no provider call during plan inspection; unavailable credentials/cloud access leave VM gates incomplete.
- [ ] On an owned native ARM64 release-compatible VM, using the same frozen archive/profiles and release BuildKit policy, prove actual named-builder selection/local loading plus real Oracle/O3/G1/JFR and Community/O3/serial compiled binary export. Inspect effective commands/options, executable architecture and actual builder facts; schema-only/mock exports do not qualify. Expected: capability PASS; unsupported policy stops and revises spec, access failure leaves an incomplete gate with compensated resources.
- [ ] Run an isolated real ARM assembly/staging-push/runtime slice using shared production helpers, not a modified release DAG or fabricated prerequisite receipts. Retain all 44 IDs/platforms, three reports/logs, inputs/archive/inventory/build facts, registry digests and all existing server/watchdog ARM smoke outcomes. Run unchanged verification/resume with retained infrastructure and prove zero assembly/push; compensate owned resources and verify unrelated AMD images/resources survive. Expected: native slice PASS with exact scope; this is not the canonical DAG verdict.
- [ ] Run `uv run --locked --package nanolab nanolab run packages/nanolab/scenarios-v2/release.yaml --environment /tmp/nanolab-release-arm64-verification/environment.yaml --release-config /tmp/nanolab-release-arm64-verification/release-config.yaml --run-dir /tmp/nanolab-release-arm64-verification/run --until test-arm64-images --keep`. Expected: genuine source tests, AMD64 build/push, three benchmarks, aggregate/regression, ARM64 build/push/smoke; zero public push/signing. Retain evidence. MFA or an existing failed regression gate is recorded without bypass/threshold changes.
- [ ] With retained canonical infrastructure run the same bounded command with `--resume --keep`; expected no assembly/push for unchanged verified phases. Run matching `--teardown` with the same scenario/environment/config/run-dir after success or failure; expected owned resources released, unrelated resources preserved and diagnostics retained. A failed earlier step still requires cleanup, not a success receipt.
- [ ] Document supported profiles, assembly/push and CLI boundary changes, exact local/native/canonical results and pending gates in README/profile docs/roadmap. Keep ARM migration unchecked until required real evidence passes; retain AMD64's independent pending gate. Expected: claims match actual logs; no qualified public release claimed.
- [ ] Review the whole branch against the spec with the native execution workflow's fresh final reviewer, fix Important/Critical findings with regression tests and green suite, record minors/rulings. Commit `Document ARM64 recipe release verification gates` unless all required real gates passed, in which case use `Document verified ARM64 recipe release builds`. Merge/push remains the user's later integration choice.

## Plan self-review and handoff

Spec coverage: exact profiles/freeze (Task 1), shared source/builder/transfer/image
proof and ownership (Task 2), VM-specific verifiers (Task 3), local/registry
receipt transition and pre-push proof (Task 4), DAG/legacy/resume/selection/removal
(Task 5), native capability/slice/canonical/cleanup and truthful documentation
(Task 6). Every Review Focus entry has a named owning regression test.

Interface checks: Task 1's ARM fields feed Task 3 routing and Task 5 DAG; Task 2's
architecture/role arguments feed Task 4 constructors; Task 4's push receipt/phase
feeds Task 5 smoke/publication. Task 4 must migrate existing AMD caller keyword
arguments to its shared interfaces in the same commit while leaving active ARM
Bake/readers intact. Task 5 switches ARM DAG and every legacy registry reader
atomically; intermediate commits remain executable without compatibility shims.
No implementation or cloud probe has run under this plan. Azure MFA and previous
AMD64 verification remain external pending gates. Execution preference is native
in this session, with one fresh whole-branch reviewer after implementation.
