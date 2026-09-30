# Runtime Comparison Recipes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in the current session. The user previously selected direct execution; preserve that method. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare `nanolab compare` with reusable recipe v2 profiles on the measured VM, then run cells against verified, fixed images.

**Architecture:** Capture one source snapshot and publish the JVM profile with both functions, followed by selected control-plane-only profiles. A versioned manifest records experiment inputs, original infrastructure and validated publications; resume reads and validates it before any mutation or provisioning. Cells reuse the prepared registry, source and distributions, with Kubernetes image verification as a prerequisite of the existing load composite.

**Tech Stack:** Python 3.12+, Pydantic, Sonata 0.6.4 resources/tasks, NanoFaaS Gradle recipes v2, Docker registry, k3s, Multipass, k6, pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-comparison-recipes-design.md` (user revised and approved for planning).

**Resolved contract constraint:** the pinned NanoFaaS recipe contract requires `build-metadata` for an explicit `controlPlane.build.native.optimization`. The user approved adding the module to every variant. The spec now uses that uniform three-module set and declares `build.variant`; no plugin or pin change is needed. Treat results as a new series relative to historical matrices.

## Global Constraints

- Implement all nine supported variants; the first end-to-end proof is `jvm` with one repetition on Multipass. Native publication remains unproven until executed.
- Build on the stack VM. Publish `jvm` once even when unselected, then each selected variant other than `jvm` once. Only the JVM profile includes the two functions.
- Exact modules: `k8s-deployment-provider`, `async-queue`, `build-metadata`. Set `build.variant` to the variant key in every comparison recipe.
- `async-queue` is still a supported module id; it supplies `per-function` scheduling to the composed engine. Preserve that strategy and public async admission for the mixed profile.
- Declare C1 explicitly for `jvm`: `-XX:+UseSerialGC -XX:TieredStopAtLevel=1`. Historical samples without an explicit tier flag are not equivalent.
- Use `127.0.0.1:5000/nanofaas`, distinct `control-plane-<variant>` image names and one unique run tag. Native G1 needs Oracle GraalVM in the VM.
- Preserve workload, resource limits, k6 scripts, Prometheus queries, interleaved order, retries, reports and cell completion criteria. Recipe selection belongs to `compare`, not the cell's `recipeProfile`.
- Resume requires identical effective inputs, the original stack VM/cluster and all recorded image identities. `--fresh` does not bypass these checks. Reject legacy or invalid manifests without overwriting them.
- No new dependencies, Sonata release or NanoFaaS code changes. Preserve unrelated dirty files in both repositories; work in `/tmp/nanolab-comparison-recipe`.

## Review Focus

1. Same YAML path with changed workload, defaults, resolved function settings or VM configuration: reject before provisioning; equivalent content at another scenario/environment path remains equivalent (Task 2).
2. A replacement VM has the original name, or k3s was recreated on the original VM: compare stable machine/cluster identities rather than names or IPs (Tasks 3 and 6).
3. An interruption lands between publication and its manifest commit: reuse committed publications, retry only uncommitted variants, and start no cell until every required entry exists (Task 4).
4. A report describes the right revision but another tracked patch, or a JVM image launches other flags: reject artifact evidence despite plausible metadata (Tasks 3 and 4).
5. A registry tag or ready Pod contains another image between preparation and load: fail the cell before k6, retaining inspection evidence (Task 5).

## File Map

- New `comparison/profiles.py`: profile selection, semantic validation, shared function mapping and the prepared-distribution handoff.
- New `comparison/manifest.py`: canonical experiment identity, strict versioned manifest, atomic persistence and publication receipts.
- New `comparison/target.py`: read-only identity probes for original machines and cluster.
- Existing `comparison/prepare.py`: one captured/staged source and ordered recipe publication; retain leftover registration cleanup and heartbeat integration.
- New `comparison/evidence.py`: source, registry, native options and JVM image inspection.
- Existing `workspace/recipe.py`, `tasks/recipe_remote.py`, `tasks/recipe.py`: reuse snapshot/bundling/command/report support with narrowly scoped additions.
- Existing `cli/comparison.py`, `cli/product.py`, `plans/runtime_comparison.py`, `plans/loadtest.py`, `tasks/loadtest/__init__.py`: orchestration and an optional prerequisite for verifying prepared images.
- Nine new `packages/nanolab/recipes/comparison-*.yaml` profiles; `.github/workflows/ci.yml`, both repository/package READMEs, recipe README, comparison scenario comments and roadmap: validation and usage.
- Tests live beside their owners in `tests/comparison/`, `tests/cli/`, `tests/tasks/`, `tests/workspace/` and `tests/plans/`. All paths above are relative to `packages/nanolab/src/nanolab/` unless fully qualified.

### Task 1: Version and resolve the comparison profiles

**Files:** create `packages/nanolab/src/nanolab/comparison/profiles.py`, `packages/nanolab/tests/comparison/test_profiles.py` and nine recipe YAML files; modify `.github/workflows/ci.yml`.

**Interfaces:**
- `comparison_profiles(tool_root: Path, variants: tuple[str, ...]) -> dict[str, Path]`: insertion order is `jvm` then selected keys excluding `jvm`. Reject empty/unknown/duplicate selections.
- `declared_options(profile: Path) -> dict[str, object]`: normalized mode, JVM argument list or effective native options, exact modules and recipe function identities.
- `PreparedComparison` frozen dataclass: `remote_source: PurePosixPath`, `distributions: Mapping[str, RecipeDistribution]`.
- `COMPARISON_FUNCTIONS` maps `word-stats-java` to `('word-stats', 'java')` and `word-stats-javascript` to `('word-stats', 'javascript')`.
- `COMPARISON_RECIPE_MODULES = frozenset({'k8s-deployment-provider', 'async-queue', 'build-metadata'})`; use it for profile/report validation, while standalone non-recipe comparison plans keep their existing module constant.

- [ ] Write parametrized profile tests asserting:
  - Keys: `jvm`, `jvm-g1`, `jvm-c2`, `jvm-g1-c2`, `jvm-loop1`, `jvm-c2-loop1`, `native-os`, `native-o3`, `native-o3-g1`.
  - Every profile has schema 2, name `comparison-<key>`, the exact three modules, build variant `<key>`, repository `127.0.0.1:5000/nanofaas` and image `control-plane-<key>`.
  - JVM args: Serial/C1; G1/C1; Serial/full; G1/full; Serial/C1 plus `-Dreactor.netty.ioWorkerCount=1`; Serial/full plus that property, respectively.
  - Native options: host builder, serial/`s`, serial/`3`, G1/`3` with effective `jfr` monitoring, respectively.
  - Only `jvm` contains `word-stats` Java JVM and `word-stats` JavaScript, with image names `java-word-stats` and `javascript-word-stats`.
  - Selection `('native-o3', 'jvm-g1')` resolves `['jvm', 'native-o3', 'jvm-g1']`; repeated/unknown keys fail.

  ```python
  def test_nonbaseline_selection_prepares_shared_functions_first():
      tool_root = Path(__file__).resolve().parents[2]
      selected = comparison_profiles(tool_root, ('native-o3', 'jvm-g1'))
      assert list(selected) == ['jvm', 'native-o3', 'jvm-g1']
      assert selected['jvm'].name == 'comparison-jvm.yaml'
  ```

- [ ] Run `uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/comparison/test_profiles.py --no-cov`; require new tests to fail first.
- [ ] Add the profiles and resolver. Keep profiles static and reusable; inspect scenario function identities before any provisioning. Extend the existing CI profile-validation step to include `comparison-*.yaml` using a shell glob, without introducing another CI job.
- [ ] Rerun profile tests. Run `validateRecipe` for all nine profiles from a disposable checkout of NanoFaaS at CI pin `e7914be065e844776af57fe9e449bce7f12e03c5`; require exit 0. Do not use the dirty primary checkout for Gradle.
- [ ] Commit Task 1 files: `Add reusable runtime comparison recipe profiles`.

### Task 2: Capture complete inputs and refuse incompatible resume

**Files:** create `packages/nanolab/src/nanolab/comparison/manifest.py`, `packages/nanolab/tests/comparison/test_manifest.py`; modify `packages/nanolab/src/nanolab/workspace/recipe.py`, `packages/nanolab/tests/workspace/test_recipe.py`. Keep existing `comparison/matrix.py` scheduling and legacy report readability.

**Interfaces:**
- `capture_comparison_inputs(*, scenario: ScenarioConfig, environment: EnvironmentConfig, nanofaas_root: Path, nanolab_root: Path, profiles: Mapping[str, Path], variants: tuple[str, ...], repetitions: int, tag: str, build_memory: str | None, parallelism: int | None) -> dict[str, object]`.
- `ComparisonManifest`: strict Pydantic model with `schemaVersion: Literal[1]`, `identity: dict[str, object]`, `target: dict[str, object] | None` (initially null), `publications: dict[str, dict[str, object]]`, and report-compatible `started_at`, `functions`, `repetitions`, `order`, `variants`, `regime`. Validate mandatory nested identity/receipt fields, not just outer dictionary types.
- `read_comparison_manifest(root: Path) -> ComparisonManifest | None`: null only for absent/empty root; a nonempty root without a complete supported manifest raises.
- `require_matching_inputs(manifest: ComparisonManifest, inputs: Mapping[str, object]) -> None`.
- `new_comparison_manifest(inputs: Mapping[str, object], cells: tuple[ComparisonCell, ...]) -> ComparisonManifest` and `write_comparison_manifest(root: Path, manifest: ComparisonManifest) -> None` (same-directory temporary file and atomic replace).

- [ ] Test immutable identity with actual small Git repos and resolved configuration. Assert revision plus SHA-256 of `git diff HEAD --binary --no-ext-diff` for both NanoFaaS and NanoLab; profile byte hashes; tag; ordered selection; repetitions; native overrides; canonical scenario/environment values and hashes; resolved function definitions, payloads and registration settings. Configuration file paths are diagnostic fields, excluded from equality. Include model defaults and resolved role targets/VM requests, not only explicitly supplied YAML fields. Fail closed when Git identity cannot be established.
- [ ] Parametrize resume rejection for changed patch, profile, load scale, mixed shares, resources, function settings, VM sizing, provider settings, NanoLab revision/patch, selection order and repetitions. Assert rejection leaves manifest bytes unchanged. Assert equivalent configurations parsed from other paths compare equal.

  Name the first regression `test_changed_load_scale_refuses_resume_without_rewriting`; after setup with an existing manifest and changed captured input, its decisive assertions are:

  ```python
  before = (root / 'comparison-manifest.json').read_bytes()
  with pytest.raises(ValueError, match='inputs'):
      require_matching_inputs(manifest, changed_inputs)
  assert (root / 'comparison-manifest.json').read_bytes() == before
  ```

- [ ] Test missing manifest in a nonempty root, malformed JSON, unsupported schema, missing identity fields and stale profile copies. Preserve compatibility of the comparison HTML reader with added fields. Add an atomic-write failure test: the prior complete manifest survives a failed replacement.
- [ ] Add a snapshot test for a tracked addition, tracked deletion and executable-bit change. Require the staged checkout's `git diff HEAD --binary` to equal the captured patch; use `git apply --index --binary` so tracked additions remain represented in its index. Do not expand capture to untracked input files.
- [ ] Run the two focused test files and `tests/comparison/test_matrix.py`; require new cases to fail, implement canonicalization/manifest operations and the snapshot correction, then rerun successfully. Record declared options and native property overrides separately; never invent a report patch hash or full JVM arguments.
- [ ] Commit Task 2 files: `Record immutable comparison experiment inputs`.

### Task 3: Stage one source and identify the original infrastructure

**Files:** create `packages/nanolab/src/nanolab/comparison/target.py`, `packages/nanolab/tests/comparison/test_target.py`; modify `packages/nanolab/src/nanolab/comparison/prepare.py`, `packages/nanolab/src/nanolab/tasks/recipe_remote.py`, `packages/nanolab/tests/comparison/test_prepare.py`, `packages/nanolab/tests/tasks/test_recipe_remote.py`.

**Interfaces:**
- `read_comparison_target(provider: VmCommandProvider, requests: Mapping[str, VmRequest]) -> dict[str, object]`: read-only probes, no ensure/install/create operations.
- `require_comparison_target(expected: Mapping[str, object], actual: Mapping[str, object]) -> None`.
- `ComparisonStage` frozen dataclass: `source: Path`, `remote_root: PurePosixPath`, `runs: Mapping[str, RecipeRun]`.
- `stage_comparison(*, source: Path, profiles: Mapping[str, Path], root: Path, tag: str, provider: VmCommandProvider, request: VmRequest) -> ComparisonStage`.
- Expose existing archive creation as `bundle_recipe_source(source: Path, destination: Path) -> None`; update validate callers/tests to use it, preserving archive exclusions and sanitized Git metadata.

- [ ] Test target records machine ID and SMBIOS product UUID for each configured VM role, plus the `kube-system` namespace UID and node name/UID map for the stack cluster. Names, addresses and boot IDs are insufficient identity. Missing/unreadable identifiers, remote failures, replacement machine with the same name or recreated cluster fail closed. Stable identity across a reboot succeeds. Keep raw probe evidence in `prepare/target.json`.

  `test_same_name_replacement_vm_is_not_the_original` passes two probe records with the same configured name and different machine/product UUIDs:

  ```python
  with pytest.raises(ValueError, match='identity'):
      require_comparison_target(original, replacement)
  assert provider.ensure_calls == []
  ```

- [ ] Test exactly one source clone/archive/upload/extraction for multiple profiles, shared `RecipeRun.source_dir`, distinct copied recipe/output paths and one owned remote root derived from `remote_recipe_root(request, tag)`. Use VM `profiles/<key>.yaml` and `distributions/<key>/`; outputs remain outside the Docker source context.
- [ ] Test staged revision, patch, tracked paths, modes and profile hashes against the captured inputs. Recheck source consistency after capture to catch edits during staging. Existing valid host staging is reused on resume; altered staged inputs fail. Re-staging the captured source is allowed when its remote directory was cleaned, without rebuilding committed images.
- [ ] Run target, prepare, remote-recipe and workspace-recipe tests; require new cases to fail, implement the probes/staging with existing VM transfer and bundling primitives, then rerun successfully.
- [ ] Commit Task 3 files: `Stage comparison source once and record VM identity`.

### Task 4: Publish each recipe and commit verified evidence

**Files:** create `packages/nanolab/src/nanolab/comparison/evidence.py`, `packages/nanolab/tests/comparison/test_evidence.py`; modify `comparison/prepare.py`, `comparison/manifest.py`, `tasks/recipe.py` and corresponding tests under `packages/nanolab/`.

**Interfaces:**
- Extend `RecipeComponent` with optional `native: dict[str, object] | None = None`; parse the report's `native` field without weakening existing readers.
- Extend `recipe_command(..., native_build_memory: str | None = None, native_parallelism: int | None = None) -> tuple[str, ...]`; emit only supplied `-PnativeBuildMemory`/`-PnativeParallelism`.
- `verify_comparison_publication(*, distribution: RecipeDistribution, stage: ComparisonStage, variant: str, inputs: Mapping[str, object], provider: VmCommandProvider, request: VmRequest, evidence_dir: Path) -> dict[str, object]`.
- `require_recorded_publications(*, manifest: ComparisonManifest, provider: VmCommandProvider, request: VmRequest, root: Path) -> None`: revalidate local report/profile/inspection hashes and registry artifacts for every committed receipt, before preparing anything else.
- `prepare_comparison(*, stage: ComparisonStage, manifest: ComparisonManifest, root: Path, provider: VmCommandProvider, request: VmRequest) -> PreparedComparison`: run an ordered Sonata preparation workflow, then return all required validated distributions.

- [ ] Test fake publication order: `jvm` then selected keys excluding `jvm`; functions only in the first publication; every command is VM `publishRecipe` using the shared checkout and its own output/log. Forward native sizing only to native profiles. No bootJar, legacy native script, Docker build or separate push commands are scheduled.
- [ ] Test report rejection for wrong recipe hash/tag/revision/dirty state/modules, wrong variant/derived optimization, wrong or extra component/SDK/mode, malformed `sha256:` digests, native optimization/collector/monitoring/builder mismatch, and published digest/config that differs from the registry. For G1 expect effective `jfr` monitoring. Require no cells after failure and retain Gradle/partial-report evidence.
- [ ] Test source evidence before **each** publication: staged tracked files and patch SHA-256 match the captured host snapshot; a correct revision/dirty flag with another patch fails. Persist source verification beside the report.
- [ ] Test JVM artifact verification by inspecting the image at its published digest, creating a stopped container, copying `/app/jvm.options` and `/app/launch.args`, and inspecting image launch configuration. Check fixed argument files, recipe argument suffix and effective collector/tier/event-loop settings; a trailing conflicting flag or altered entry point fails. Remove only the temporary inspection container in `finally`; never run a warmup workload. Persist raw files/config and separate declared options from verified artifact evidence.
- [ ] Test crash boundaries: after successful publication but before receipt commit the variant may be retried; after receipt commit it is reused without publication. Require every publication receipt before any cell. Preserve committed manifest entries and report hashes on failure. A committed artifact missing from the registry fails without republishing.

  `test_resume_reuses_committed_jvm_and_publishes_only_remaining_variant` uses a recording fake provider with a valid JVM receipt and uncommitted `native-o3`:

  ```python
  assert published_variants == ['native-o3']
  assert set(prepared.distributions) == {'jvm', 'native-o3'}
  assert original_jvm_receipt == resumed_manifest.publications['jvm']
  ```

- [ ] Run `uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/comparison packages/nanolab/tests/tasks/test_recipe.py packages/nanolab/tests/tasks/test_recipe_remote.py --no-cov`, observe new failures, then implement using `read_distribution`, exact-set validation, `run_remote_logged`, `VmFileFetcher` and registry manifest/config inspection. Each receipt includes relative evidence paths and SHA-256 hashes, profile/source identity, image references/digests, declared options and forwarded properties. Key its image records by `control-plane`, `word-stats/java` and `word-stats/javascript` as applicable. Commit it only after verification passes.
- [ ] Rerun the same suites successfully. Commit Task 4 files: `Publish and verify comparison recipe distributions`.

### Task 5: Bind cells to distributions and check images before k6

**Files:** modify `packages/nanolab/src/nanolab/plans/runtime_comparison.py`, `plans/loadtest.py`, `tasks/loadtest/__init__.py`; modify `tests/plans/test_runtime_comparison.py`, `tests/plans/test_loadtest.py`, `tests/tasks/migrated/test_loadtest.py`; create `packages/nanolab/tests/comparison/test_cell_images.py`.

**Interfaces:**
- Extend `build_runtime_comparison_plan(..., prepared: PreparedComparison | None = None) -> Workflow`.
- Extend `build_loadtest_plan(...)` and `build_loadtest_workflow(...)` with `before_load: Callable[[Platform], Resource[None]] | None = None`. Acquire the returned resource after platform/function readiness and make the existing load composite require it. Existing callers retain their behavior.
- The prepared comparison uses the selected variant distribution for the control plane and the `jvm` distribution for both functions; its remote Helm/source path is `prepared.remote_source`.

- [ ] Test report-derived image references override legacy constructed tags; both function keys map to the shared JVM report across two different variants. Assert no build/push operations in a cell and no `recipeProfile` assignment.
- [ ] Test exact existing k6 script/environment, `NO_STAGES`, metric catalogue, cAdvisor settings, heap-metric optionality, resource limits and post-load tasks. Prepared cells include `build-metadata` in their declared observed module set without adding new measurement queries. Existing standalone comparison plans and other load tests retain their current behavior when `prepared` is absent; keep their two-module constant separate from the three-module recipe contract.
- [ ] Test registry retagging, wrong runtime build metadata and a wrong ready Pod image block k6. Construct two constant distribution resources and reuse `RecipeMetadataCheckTask` against the platform endpoint, then `RecipeKubernetesImageCheckTask` for control plane and each `fn-<resolved-function-name>` deployment, role `stack`, namespace `DEFAULT_NAMESPACE`. Use separate per-SDK evidence directories to avoid both `word-stats` components overwriting the same file. Require metadata and all three image checks to finish before load.

  `test_wrong_ready_pod_image_prevents_load` supplies ready Pod/CRI evidence with a manifest different from the selected report:

  ```python
  with pytest.raises(ValueError, match='manifest differs'):
      workflow.run()
  assert k6_calls == []
  assert image_evidence_path.is_file()
  ```

- [ ] Use report references with the unique run tag: current Helm `_image_parts`/chart wiring expects repository plus tag and does not support digest-qualified control-plane references. Verify the registry manifest for each tag immediately before the cell, then verify Pod/CRI config and manifest identities with the existing check task. Do not change the chart contract in this slice.
- [ ] Run focused runtime-comparison/loadtest/Kubernetes-image suites, require new failures, implement the optional prerequisite and prepared-distribution branch, then rerun successfully. Verify one failed check releases acquired cell resources and never invokes k6.
- [ ] Commit Task 5 files: `Verify prepared comparison images before load`.

### Task 6: Route compare through immutable preparation and safe resume

**Files:** modify `packages/nanolab/src/nanolab/cli/comparison.py`, `packages/nanolab/src/nanolab/cli/product.py`; create `packages/nanolab/tests/cli/test_comparison.py`; modify existing comparison/heartbeat tests as necessary.

**Interfaces:**
- `_run_prepare(...) -> PreparedComparison` delegates to Tasks 3–4 and retains console progress/heartbeat.
- `_run_cell(..., prepared: PreparedComparison) -> None` passes prepared artifacts through the product execution path; retain `CELL_ATTEMPTS = 2`.
- Add `prepared_comparison: PreparedComparison | None = None` to `_execute_workflow`, `_build_run_workflow` and `_workflow`. Only the runtime-comparison builder receives it; bind its stack commands and Helm chart to the captured remote source.
- Add `provision: bool = True` to `_execute_workflow`; compare cells pass `False`, while normal `run` callers keep existing provisioning.

- [ ] Test early CLI rejection for non-Kubernetes/non-comparison scenario, unsupported local comparison environment, wrong function pair, explicit conflicting prebuilt images/recipe profile, empty/duplicate variant selection and invalid repetition/native parallelism values. Preserve managed VM environments and their existing role bindings; no new backend/provider capability is implied.
- [ ] Test a new run: preflight and capture inputs → atomic initial manifest → provision once with `keep=True` → record target identities atomically → stage/publish/verify → all cells → existing comparison HTML report. Recheck host input identity before each cell to catch source/config edits during the run. No cell repeats provisioning, repository sync or cluster setup.
- [ ] Test resume: read/validate manifest and effective inputs before writing/provisioning; probe recorded target and every committed image; skip provisioning/bootstrapping; stage captured inputs if necessary; prepare only remaining variants; select pending cells. An existing manifest without recorded target identity cannot be recovered automatically. Missing/changed machine, cluster, evidence or image fails without rebuilding recorded variants or touching completed results.
- [ ] Run the same negative resume tests with `--fresh`; assert it selects all cells only after the same checks. Preserve manifest start time, publications and earlier results during ordinary resume. The report reads the original supported fields and tolerates added evidence.

  `test_fresh_cannot_replace_original_cluster` invokes the CLI with a valid unchanged input manifest and a changed cluster UID:

  ```python
  assert result.exit_code != 0
  assert provisioning_calls == publication_calls == k6_calls == []
  assert manifest_path.read_bytes() == original_manifest_bytes
  ```

- [ ] Test two cells and one retry: same source/functions and verified registry, interleaved ordering, existing cleanup behavior, no duplicate preparation, no retry that bypasses image verification. Retain leftover registration cleanup before pending cells. Success cleans only owned remote staging; failure keeps diagnostics and the original VM for resume.
- [ ] Run CLI/comparison/heartbeat and representative product-command tests, require new failures, wire the sequential orchestration, then rerun successfully. Remove obsolete comparison-only build helpers/imports only after confirming no remaining callers; retain any legacy helper still used by standalone workflows.
- [ ] Commit Task 6 files: `Run comparison matrices from verified recipe artifacts`.

### Task 7: Verify the JVM path on Multipass and document its limits

**Files:** modify `README.md`, `packages/nanolab/README.md`, `packages/nanolab/recipes/README.md`, `docs/recipes-roadmap.md`, and outdated build/tag comments in `packages/nanolab/scenarios-v2/runtime-comparison*.yaml`. Preserve scenario values.

**Interfaces:** keep CLI `compare SCENARIO --environment ENV --variants ... --repetitions ... --run-dir ...`. Artifacts: `comparison-manifest.json`, captured source/profiles, `prepare/<variant>/distribution/distribution.json`, Gradle/source/JVM/registry evidence, per-cell Pod evidence, k6 summary, metrics snapshot and comparison report.

- [ ] Run final profile validation against the pinned disposable NanoFaaS checkout; require all nine `validateRecipe` calls to exit 0. Record actual checkout revision and commands.
- [ ] Run from the isolated NanoLab worktree:

  ```bash
  NANOFAAS_ROOT=<disposable-pinned-checkout> ./nanolab.sh compare \
    packages/nanolab/scenarios-v2/runtime-comparison-jvm.yaml \
    --environment packages/nanolab/environments/multipass.yaml \
    --variants jvm --repetitions 1 \
    --run-dir /tmp/nanolab-comparison-recipe-e2e-<unique-id>
  ```

  Require VM creation when absent, one source stage/publication, verified C1 artifact and three running image identities before k6, existing measurements and `comparison-report.html`. Retain the original VM because `compare` uses `keep=True`; never globally purge Multipass or remove an unrelated existing VM.
- [ ] Rerun the same command against the retained VM and run directory. Require zero publications and zero completed-cell loads; verify identities and regenerate the report. Before runtime execution stops being available, exercise an input mismatch with `--fresh` and verify early refusal leaves the manifest/results unchanged. Do not count fake native tests as native execution evidence.
- [ ] Run `uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests` (existing coverage gate), `uv run --locked --all-packages --all-groups ruff check packages`, `uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab`, `uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache`, formatting checks for changed Python files, and `git diff --check`. Fix regressions introduced by this branch.
- [ ] Document profiles, artifacts, the always-prepared JVM/functions distribution, C1 and added-module historical-sample caveats, retained VM, strict original-infrastructure resume, legacy-run refusal and `--fresh`. Update the roadmap with the JVM evidence path/date and explicitly leave native publication/Oracle G1 and other load-test backends open.
- [ ] Commit Task 7 files: `Document verified recipe runtime comparison`. Record executed commands/results and evidence paths below; keep failures and native limitations explicit. Implementation completion requires actual evidence, not this plan's expected results.

## Execution Evidence

Execution has not started. Populate this section during Task 7 with actual profile-validation, test, Multipass and resume results.
