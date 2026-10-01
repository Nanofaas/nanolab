# AMD64 Release Recipes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Assemble the guarded release's AMD64 images with three reusable recipes, preserving separate push, source identity and resumable release evidence.

**Architecture:** Freeze profiles against the source-derived `ImagePlan` during preflight. Run three `assembleRecipe` commands on the stack VM with the owned builder, then adapt verified reports and independently inspected local image IDs into the existing AMD64 receipt. Registry push and subsequent release phases keep their contracts.

**Tech Stack:** Python >=3.12, existing uv workspace, Sonata 0.6.4 resources/executors, NanoFaaS Gradle recipes v2, Docker/Buildx/BuildKit, existing Azure release infrastructure. No new dependency, public command or generic task.

**Spec:** `docs/superpowers/specs/2026-10-01-release-amd64-recipes-design.md`

## Global Constraints

- Verify against NanoFaaS `e7914be065e844776af57fe9e449bce7f12e03c5`; baseline NanoLab `0ddf589`, spec commit `0016b31`.
- Native Linux AMD64 stack VM; reject host/daemon architecture mismatch and emulation as qualifying evidence.
- Three profiles, three root `assembleRecipe` invocations, exact original names/tags: JVM 9, native 12, default 23 at the current pin. Derive expected coverage from the guarded source.
- Schema 2; registry `127.0.0.1:5000/nanofaas`; omit `registry.platforms` and `registry.provenance`.
- Explicit modules: async-queue, autoscaler, build-metadata, concurrency-control, k8s-deployment-provider, offload, runtime-config, sync-queue.
- JVM G1/C2, CP variant `jvm-g1-c2`; Spring native container builder/Oracle/O3/G1/effective JFR, CP variant `native-o3-g1`; Java-lite Community/O3/serial.
- Default profile's sole artifact-only CP is JVM without `container`; allow its `image: null` only in the release adapter.
- Retain clean/prepared-source guards and verified Git archive; never substitute dirty or synthetic Git staging. `source: null` in a report is not commit proof.
- Owned docker-container builder, `default-load=true`, `BUILDX_BUILDER`, BuildKit enabled; retain configured BuildKit `max_parallelism` (current scenario 2), without conflating it with native compiler parallelism.
- Local build: `local-image-digest`/`docker-daemon:`. Staging push: `local-registry-digest`/`docker://`. No GHCR or signing during verification.
- Preserve source tests, three benchmarks, regression policy, ARM64 Bake/smoke, publication and signing barriers. Leave smoke thresholds, P24 and other backends for separate work.

## Review Focus

1. Source additions hidden in ignored paths or symlink changes: reject them against the archive inventory, including committed files below permitted output directories (Task 4).
2. Old successful reports or tags surviving a failed assembly: require fresh group output and full successful command/report/inspection agreement (Tasks 3–4).
3. Dynamic SDK/service catalogs or module additions: reject profile drift before any provider acquisition, without dropping cells (Task 2).
4. Deleted report files or replaced local tags during resume: invalidate the build and dependent receipts without rebuilding just to verify (Task 5).
5. Transfer failure/cancellation after one group and cleanup failures: retain diagnostics, issue no complete receipt and compensate only owned paths/resources (Task 4).

## Working context and file responsibilities

Use branch `feature/recipe-release-amd64` in `/tmp/nanolab-recipe-multiarch`.
Preserve the unrelated dirty files in the primary NanoLab checkout. Execution
method already chosen by the user: native, in this session.

Set `NANOFAAS_ROOT=/home/michele/Documenti/nanofaas/.worktrees/nanolab-merge-e7914be0`.
Test commands run from the worktree with `uv run --locked --package nanolab`;
request socket/network access only for checks that need it. Baseline full suite:
3145 passed, log `/tmp/nanolab-release-amd64-baseline-tests.log`.

- Create `packages/nanolab/recipes/release-amd64-{jvm,native,default}.yaml`: reusable source-owned selections.
- Create `packages/nanolab/src/nanolab/release/recipe.py`: immutable profile groups, matrix compatibility and strict release report adaptation.
- Create `packages/nanolab/src/nanolab/release/recipe_execution.py`: archive inventory checks, commands, complete evidence collection and image verification.
- Modify `release/resources.py`: stage frozen recipe/BuildKit inputs and compensate owned remote inputs; preserve ARM64 resources.
- Modify `plans/release.py` and `plans/release_phases.py`: freeze inputs before acquisition and replace only the AMD64 build path.
- Modify `release/build.py`: remove the unused AMD64 prerequisite/Bake generator once callers migrate.
- Modify `release/evidence.py` or `release/tasks.py` only where required to verify new file evidence; retain digest schemes and existing receipts.
- Add tests in `packages/nanolab/tests/release/test_recipe.py` and `test_recipe_execution.py`; extend existing release/CLI/DAG tests, CI profile validation, README and roadmap.

## Task 1: Establish toolchain and recipe capability

**Files:** Read the approved spec, `tasks/recipe.py`, `release/build.py`, `images/plan.py`, installed Sonata `buildx_builder_resource`, and the pinned NanoFaaS recipe implementation. Update this plan with a verdict and retained evidence paths. Keep disposable probes outside product source.

**Interfaces:** Consumes the pinned source and installed release toolchain. Produces a PASS capability record containing exact profile selections, module list, native task/binary mapping, Maven staging requirements, effective native options and builder/load/export observations. Local implementation depends on the verified profile/catalog checks; native-AMD64 builder/export evidence remains a final completion gate in Task 6.

- [x] Inspect named NanoFaaS symbols/flows through GitNexus query/context first as its AGENTS instructions require; verify the pinned implementation directly where the graph is unresolved. Make no NanoFaaS/plugin edits.
- [x] Materialize the three candidate profiles under `/tmp/nanolab-release-amd64-capability/`, using Task 2's exact selections. Validate each with `./gradlew validateRecipe -Precipe=<absolute-profile>` in the pinned source; confirm the default artifact-only CP and all Java-lite selections are accepted.
- [x] Confirm the effective Java-lite native task/output binary, Community/O3/serial build, Spring Oracle/O3/G1/JFR settings and resolved eight-module selection. Confirm no containerd Maven staging is required: `all` prioritizes the default Kubernetes provider and excludes its two conflicting providers.
- [ ] On a provisioned native-AMD64 release-compatible stack VM, record Docker/Buildx/BuildKit versions and create an owned builder with the release configuration and `default-load=true`. Run ordinary `docker build` with `BUILDX_BUILDER` and BuildKit enabled, proving execution on that builder and automatic local image loading. Preserve any preexisting selected builder.
- [ ] From a verified archive, run representative JVM, actual Oracle G1 native and Java-lite builds through recipes. Verify native-builder explicit local export yields the compiled executable and the final tagged images are local Linux/AMD64 images. Inspect effective build commands/options, not only requested YAML. No emulation or fake exporter artifact qualifies.
- [ ] Retain raw commands, profile hashes, logs, builder facts, exported binary/runtime observations and ownership/cleanup results. Record PASS or the exact unsupported contract in this plan. If VM/toolchain access is unavailable, leave this gate incomplete; if capability fails, stop and revise the spec. Never fall back to Bake or a Docker wrapper.
- [ ] Commit the textual capability record as `Record AMD64 recipe build capabilities`; exclude generated outputs and secrets.

Expected: demonstrated named-builder selection, local loading and real native export/packaging for both native policies. Schema validation alone is insufficient. After profile/catalog verification, local implementation may proceed; do not claim migration complete before the deferred native-AMD64 gate passes.

### Capability progress — 2026-10-01

- Gradle `validateRecipe` passed for all three full candidate profiles after
  correcting the explicit selection to eight modules; counts remain 9/12/23.
- A real `-PcontrolPlaneModules=all` Gradle probe resolves exactly the same
  eight modules. All three Java-lite `nativeCompile.outputFile` paths were
  read from the configured tasks. Container-built Java-lite uses Community;
  Spring G1 uses Oracle and effective JFR. No containerd dependency is selected.
- Ruling: the original spec confused the ten-module catalog with the resolved
  `all` selection. Preserve the legacy eight-module selection, including the
  Kubernetes provider, and correct the spec/plan; no backend policy changes.
  If wrong, the release module set could change; actual Gradle output verifies
  this correction.
- Candidate profiles, exact matrix, selection/native-binary output and validation
  logs: `/tmp/nanolab-release-amd64-capability/`. Source archive identity is
  recorded in `source-identity.json`; inputs remain disposable capability probes.
- Azure authentication was renewed, read-only VM/image queries passed, but the
  actual provision failed with `401 RequestDisallowedByAzure` requiring MFA.
  `provision.log` retains the failure. Failed-acquire teardown returned 0;
  a subsequent Azure resource query found no release resources remaining.
- Builder load/export and runtime verification remain **incomplete**. The real VM portion of Task 1
  is deferred to Task 6 after the user asked to continue local implementation. The user has been asked to
  renew authentication with MFA before the native-AMD64 probe continues.

## Task 2: Freeze reusable profiles against the guarded matrix

**Files:** Create the three profiles, `release/recipe.py` and `tests/release/test_recipe.py`; modify `plans/release.py` and `.github/workflows/ci.yml`.

**Interfaces:** Define frozen `ReleaseRecipeGroup` with `flavor: ImageFlavor`, `name: str`, `profile_bytes: bytes`, `profile_digest: str`, `tag: str`, `cells: tuple[ImageCell, ...]`, `modules: tuple[str, ...]`. Add `prepare_release_recipe_groups(source_tree: Path, image_plan: ImagePlan, *, profiles_root: Path) -> tuple[ReleaseRecipeGroup, ...]`. Extend `ReleaseRequest` with `recipe_groups: tuple[ReleaseRecipeGroup, ...] = ()`; the normal request builder always populates it before workflow construction. Existing direct test requests must supply validated groups rather than silently selecting legacy builds.

- [x] Write `test_release_profiles_cover_exact_guarded_matrix`: groups have flavors `(jvm, native, default)`, counts `(9,12,23)` at the pin, total 44, original image references/tags, no duplicates; default CP produces no image. Assert the eight resolved modules and the spec's per-family native/JVM settings.
- [x] Write parameterized `test_release_profile_drift_fails_before_acquisition`: add/remove a catalog cell or module, alter a SDK/service identity, repository/tag/mode/variant/options, introduce platforms/provenance or duplicate target. Assert preflight fails with zero provider calls. Write `test_profile_freeze_uses_raw_bytes`: later file mutation changes neither frozen bytes nor their SHA-256 identity; a subsequent request gets a different identity.
- [x] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/release/test_recipe.py -q --no-cov`; expect failures for absent interfaces or missing coverage checks.
- [x] Implement the group interface and strict YAML-to-matrix mapping. Normalize tags using `image_plan.version`; map catalog `exec` to `bash`, Java-lite to native, watchdog/warm-echo to service identities from the pinned schema. Freeze raw bytes, not YAML reserialization. Match resolved `all` selection from the extracted guarded source, with no `all` sentinel.
- [x] Add JVM profile CP + seven Java functions + warm-echo; native profile the same nine + three Java-lite functions; default profile six Bash/five Go/five JavaScript/six Python + watchdog and the artifact-only JVM CP. Use source-owned defaults except effective options pinned by the spec. Explicit image names come from `ImagePlan`, never inferred from report order.
- [x] Call preparation from `build_release_request` after archive-derived `ImagePlan` creation and before any acquisition. Add all three `validateRecipe` calls to the existing pinned-source CI profile gate.
- [x] Validate all profiles with pinned Gradle, then run `tests/release/test_recipe.py`, `tests/plans/test_release.py` and `tests/cli/test_release_command.py`. Expect profile validation and tests to pass.
- [x] Commit as `Add frozen AMD64 release recipe profiles`.

## Task 3: Adapt strict distribution reports into release images

**Files:** Modify `release/recipe.py`; extend `tests/release/test_recipe.py`; reuse scalar/digest helpers from existing recipe code without relaxing other consumers.

**Interfaces:** Add `read_release_distribution(report: Path, *, group: ReleaseRecipeGroup) -> tuple[RecipeComponent, ...]`, returning only fully verified image-bearing components. `RecipeComponent` is the existing type from `tasks/recipe.py`; validate the default artifact-only CP separately instead of adding nullable images to that shared DTO.

- [ ] Write `test_release_distribution_maps_all_component_kinds`: valid reports produce exactly the group's cells; `exec`/`bash`, default/container mode and services map correctly. Assert returned components preserve local image IDs, native distribution/options and CP variant. For default, assert 23 image components and a separately checked CP with `image: null`.
- [ ] Write parameterized `test_release_distribution_rejects_identity_or_matrix_mismatch`: wrong schema/hash/name/tag/modules, duplicate/extra/missing component, foreign image, absent/malformed ID, failed image status, wrong mode/variant/distribution/GC/optimization/JFR and null image on any other component all raise `ValueError`. Include duplicate JSON keys, truncated JSON and absent fields. Reject reports that claim publication instead of the required assembly state.
- [ ] Write `test_null_archive_source_is_not_commit_evidence`: `source: null` is acceptable only with independent archive verification by the execution layer; a claimed non-null source must match guarded source identity, never a synthetic revision. Preserve existing strict `read_distribution` behavior in regression tests.
- [ ] Run the new report tests and confirm failures. Implement the interface using actual report field names observed in Task 1; no broad exception catches or permissive filtering. Require the exact declared artifact-only CP metadata/modules/native absence, not merely a null image.
- [ ] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/release/test_recipe.py packages/nanolab/tests/tasks/test_recipe.py packages/nanolab/tests/tasks/test_recipe_validation.py -q --no-cov`; expect all pass.
- [ ] Commit as `Validate recipe distributions for release images`.

## Task 4: Execute recipe groups with archive and local-image evidence

**Files:** Create `release/recipe_execution.py` and `tests/release/test_recipe_execution.py`; modify `release/resources.py`, existing source/resource tests and source-phase wiring where needed.

**Interfaces:** Add `release_recipe_commands(groups: tuple[ReleaseRecipeGroup, ...], *, source_dir: str, remote_root: str, builder_name: str) -> tuple[CommandTaskSpec, ...]`. Add `run_release_recipe_steps(inputs: TaskInputs, *, groups: tuple[ReleaseRecipeGroup, ...], executor: RoleBoundCommandTaskExecutor, provider: object, request: object, source_dir: str, remote_root: str, evidence_dir: Path, inventory_file: Path, source_commit: str, archive_digest: str) -> tuple[Evidence, ...]`. Add `release_recipe_inputs_resource(*, groups: tuple[ReleaseRecipeGroup, ...], max_parallelism: int, run_dir: Path, remote_root: str, provider: object, request: object, requires: tuple[Resource[Any], ...] = ()) -> Resource[Path]`, returning the local retained BuildKit configuration path. Use existing provider/transport contracts; no new generic task.

In `recipe_execution.py`, define `capture_release_inventory(source_tree: Path, destination: Path) -> Path` and `verify_release_source(*, inventory_file: Path, provider: object, request: object, source_dir: str) -> None`. The first writes a canonical inventory from the extracted planning tree; the second compares remote entries and raises on mutation. Retain file types/modes/content hashes and symlink targets, with no Git requirement. Use these before source tests and at the assembly boundaries.

- [ ] Write `test_release_recipe_commands_use_owned_builder_and_paths`: exactly three root `assembleRecipe` commands, role `stack`, cwd staged source, `DOCKER_BUILDKIT=1`, `BUILDX_BUILDER=<owned>`, original tags, no Bake/direct bootJar/publish/retag. Profiles: `<remote_root>/recipe-inputs/amd64/<name>.yaml`; distinct outputs: `<remote_root>/recipe-output/amd64/<flavor>/`. Keep all outside source context. Use `recipe_command`; platform-less `recipeBuilder` is not the selector.
- [ ] Write `test_archive_inventory_rejects_source_mutation` covering committed-file bytes/modes/deletion, symlink target/escape and added ignored source files. Inventory the archive-derived planning tree before source tests, retain canonical path/type/mode/content-hash or link-target records plus digest. Exempt only `.gradle` at the source root and `build` under actual Gradle project paths identified from the pinned source; any original tracked entry there still requires exact verification. Any additional writable outputs found in Task 1 require explicit source-derived rules and tests.
- [ ] Write `test_complete_reports_match_independent_daemon_inspection`: every tag is independently Linux/AMD64 and ID-equal to its report; output includes 44 `local-image-digest` entries with `docker-daemon:` references plus file-digest evidence for profiles/reports/inventory/build configuration/logs. Wrong OS/architecture, replaced tag or registry digest masquerading as local ID fails.
- [ ] Write `test_failed_group_cannot_reuse_stale_output`, `test_transfer_truncation_prevents_receipt`, and parameterized `test_partial_recipe_failure_compensates_owned_inputs`: first group succeeds then failure/cancellation/transfer error; no complete receipt, diagnostics retained, compensation removes only owned remote paths/builder and preserves unrelated containers/volumes/selected builder. Cleanup failure must be reported without losing the original error.
- [ ] Run new tests and confirm failures. Implement source inventory collection/checking before source tests and immediately before/after assembly. Bind it to the verified source archive and guarded commit; recheck staged frozen profile bytes. Never call Git-based `prepare_recipe_run` or create `.git` in archive staging.
- [ ] Implement staging of frozen profiles/BuildKit config, root Gradle execution and bounded complete report/log retrieval. Empty each owned group output before that group's command; require successful exit before report validation. Use the resolved module set from Task 1; it needs no containerd Maven staging. Retain evidence locally before phase completion; compensation must preserve retained evidence for resume and review.
- [ ] Independently inspect all images and enforce exact union coverage before returning evidence. Record builder driver/environment/configuration and real installed toolchain facts as retained files. Let existing registry push obtain manifest digests later.
- [ ] Run `tests/release/test_recipe_execution.py`, `tests/release/test_resources.py`, `tests/release/test_build.py` and `tests/tasks/test_recipe_remote.py`; expect all pass. Commit as `Assemble AMD64 recipes with verified release evidence`.

## Task 5: Integrate AMD64 assembly into the resumable release DAG

**Files:** Modify `plans/release.py`, `plans/release_phases.py`, `release/build.py`; extend `tests/plans/test_release.py`, `tests/release/test_build.py`, `test_tasks.py`, `test_evidence.py` and `tests/cli/test_release_command.py`. Modify evidence verification only when new evidence cannot use existing `file-digest` verification.

**Interfaces:** Extend existing keyword-only `build_amd64_phase` with `recipe_groups: tuple[ReleaseRecipeGroup, ...]`, `provider: object`, `request: object`, `inventory_file: Path`, `archive_digest: str`; keep its `(tuple[str, ...], ReleasePhaseTask)` result. Use Task 4's execution/resource interfaces. `archive_digest` is the frozen Git archive SHA-256, verified equal to the acquired/staged archive; source inventory is frozen from the same tree. Existing registry push interfaces remain unchanged.

- [ ] Write `test_amd64_recipe_dag_preserves_release_boundaries`: source-tests -> AMD64 assembly -> staging push -> three benchmarks; builder has `default-load=true` and retained parallelism ceiling; sources remain acquired for benchmark consumers. Assert ARM64 still stages Bake/config and runs existing smoke; GHCR/Cosign remain later behind the same barriers. Assert AMD64 no longer stages Bake or prepares artifacts outside recipes.
- [ ] Write `test_recipe_phase_fingerprint_binds_all_inputs`: changing profile bytes/tag/source archive/inventory/cell options/commands/builder/driver options/configuration/parallelism changes the phase reuse key. Bind environment and cwd alongside command argv, not argv alone; prerequisite source-test receipt binds archive evidence.
- [ ] Write `test_recipe_resume_verifies_files_and_images_without_build`: unchanged verified outcome reuses the phase with zero Gradle/push calls; tampered/deleted report/profile/inventory or replaced image ID invalidates it. A partial outcome never records a complete receipt. Write `test_recipe_receipt_change_invalidates_push_and_benchmark`: changed build prerequisite digest prevents downstream reuse; preserve existing local ID versus registry digest verifier tests.
- [ ] Run focused tests and confirm failures. Replace only AMD64 resources/work; preserve ARM64 `build_inputs_resource`. Compute deterministic profile/source/inventory/config hashes before acquisition; verify acquired bytes before work. Pass provider-bound transfer/image verification through the existing stack role. Wire inventory checks into real source staging/test boundaries.
- [ ] Remove `amd64_build_commands` once graph/text caller checks in NanoLab show no remaining use; update Bake-specific AMD64 tests to check recipe contracts. Retain ARM64 Bake renderer and native source defaults; update obsolete scenario comments describing AMD64 Bake concurrency without changing values.
- [ ] Run `uv run --locked --package nanolab pytest packages/nanolab/tests/release packages/nanolab/tests/plans/test_release.py packages/nanolab/tests/cli/test_release_command.py packages/nanolab/tests/tasks/migrated/test_release_composites.py -q --no-cov`; expect all pass.
- [ ] Commit as `Use recipes in the AMD64 release build phase`.

## Task 6: Verify the native-AMD64 release slice and document evidence

**Files:** Update `README.md`, `docs/recipes-roadmap.md` and this plan with actual evidence and incomplete gates. Extend `tests/release/test_benchmark.py` only if Task 5 does not exercise image selection through the recipe receipt.

**Interfaces:** Consumes the complete executable release DAG and unchanged CLI selection. Produces retained native-AMD64 build/push/runtime/ownership evidence and a clearly bounded migration verdict; no qualified public release.

- [ ] Run `uv run --locked --package nanolab pytest packages/nanolab/tests -q`; expect all pass. Run `uv run --locked --package nanolab nanolab-quality`; expect `Quality checks passed`. Run `uv run --locked --package nanolab ruff format --check --config packages/nanolab/pyproject.toml packages/nanolab`; expect all files formatted. Run `uv run --locked --package nanolab bandit -r packages/nanolab/src/nanolab -c packages/nanolab/pyproject.toml -f json`; expect only the four preexisting B101 lows (nonzero exit is retained and reported), with no new findings. Retain complete logs and exit statuses.
- [ ] Prepare private real Azure environment/credential config under `/tmp/nanolab-release-amd64-verification/`, using the existing examples/contracts; validate actual credentials and network/VM availability without logging secrets. Use a clean prepared source checkout and unchanged release version/policy; do not fake source-test receipts or bypass execution guards. Confirm CLI `--until` selects the slug below in a contract test before incurring cloud work.
- [ ] Run `uv run --locked --package nanolab nanolab run packages/nanolab/scenarios-v2/release.yaml --environment /tmp/nanolab-release-amd64-verification/environment.yaml --release-config /tmp/nanolab-release-amd64-verification/release-config.yaml --run-dir /tmp/nanolab-release-amd64-verification/run --until push-amd64-images-to-local-registry --keep`. Expect genuine source tests, three assemblies and separate staging push; no benchmark/publication/signing executed. Retain all 44 local IDs/platforms, all 44 registry manifest digests, three raw reports, source inventory/archive binding, profiles, builder/export/log evidence and receipts. Shared source prerequisites may acquire ARM64 infrastructure.
- [ ] Exercise representative pinned runtime images on the retained stack VM: JVM control plane and Java function, Oracle G1 native control plane/function, Java-lite native function, watchdog and Bash/Go/JavaScript/Python Dockerfile functions. Use existing HTTP/invocation/service smoke contracts with expected outputs and clean up the owned probe workloads. Verify benchmark image selection resolves the unchanged native/G1 variant and staging manifest digests through the normal receipt adapter; fixtures alone do not qualify this boundary.
- [ ] With retained infrastructure, run the same prefix with `--resume --keep`; expect no assembly/push commands for unchanged verified phases. Save journal verifier observations. Demonstrate report-file and local-tag invalidation in controlled tests, retaining the successful real evidence rather than corrupting it.
- [ ] Run the matching `nanolab run ... --teardown` invocation with the same scenario/environment/config/run directory. Confirm owned resources are released and unrelated host/VM resources preserved. If an earlier step fails, still compensate/teardown and retain partial logs; leave real verification incomplete.
- [ ] Document profiles, assembly versus push, exact verified scope/results and remaining ARM64 migration in README/roadmap. Keep smoke-limit review deferred. Record any unavailable native-AMD64 gate explicitly; do not mark migration complete until it passes, and do not claim full release qualification.
- [ ] Review the complete branch against the spec, run `git diff --check`, and commit as `Document verified AMD64 recipe release builds`. Report any concrete unresolved risks before integration; merge/push follows the user's chosen integration step.

## Plan self-review and handoff

Coverage: profiles/matrix (Task 2), archive and builder/export capability (Tasks
1/4), strict distribution/local IDs (Task 3/4), DAG/resume/digest semantics
(Task 5), native VM/runtime/cleanup and documentation (Task 6). Each Review Focus
entry has named negative tests. No product implementation or capability probe
has been run for this plan; baseline tests precede these changes.

Written spec approved. Review this plan before native execution; Local profile/catalog checks from Task 1 passed; its real VM builder/export
checks must pass with Task 6 before completion. Preserve the user's existing native execution preference.
