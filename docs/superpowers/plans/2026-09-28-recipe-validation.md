# Recipe-backed Container Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, or superpowers:subagent-driven-development if the user selects delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing NanoLab container lifecycle scenario using images built and published from a reusable NanoFaaS recipe v2.

**Architecture:** Two alternative NanoLab tasks wrap `assembleRecipe` and `publishRecipe`, sharing report parsing. A resource supplies the resulting distribution to Compose and function registration at execution time. The initial validate workflow uses publication to its local registry, then proves which images actually ran.

**Tech Stack:** Python 3.12+, existing Sonata 0.6.2 packages, Pydantic, PyYAML, Gradle, local Docker/Compose.

**Spec:** `docs/superpowers/specs/2026-09-28-recipe-validation-design.md`

## Global Constraints

- No changes to the NanoFaaS plugin or Sonata dependencies.
- `AssembleRecipeTask` and `PublishRecipeTask` are alternatives; never concatenate them.
- Keep `NANOFAAS_ROOT` read-only, including Gradle outputs and caches.
- Initial scenario support: validate, container backend, local environment, Docker builds, single platform.
- Recipe owns build settings; scenario owns registration, payloads and resource assertions.
- Keep profiles under `packages/nanolab/recipes/`; generated evidence belongs to the run directory.
- Planning/dry-run must not build, start containers or stage sources.
- Preserve existing non-recipe workflows and compensation semantics.
- Graph impact is required before symbol edits. Previously `build_validate_plan` was HIGH risk (CLI/TUI callers); refresh/recheck the stale index and report current risk before implementation.

## Review Focus

1. Same tag points at a replaced local image: runtime ID verification must fail (Task 4).
2. Failed publish leaves a plausible report: never supply it to deploy (Task 2).
3. Dirty sources include deleted files, executable bits or paths with spaces: staged build must reproduce them without modifying the original (Task 1).
4. Scenario loaded outside the repo/TUI serialization loses its base directory: retain the resolved profile path (Task 3).
5. Failure during partial acquisition leaks a function or Compose project: compensation still releases acquired resources (Task 4).

## File Map

Paths below are relative to `packages/nanolab/` unless explicitly rooted at repository `docs/`.

- `src/nanolab/workspace/recipe.py`: source/profile staging and provenance.
- `src/nanolab/tasks/recipe.py`: distribution records, report reader, two build tasks and distribution resource.
- `src/nanolab/tasks/recipe_validation.py`: report-backed registration and runtime identity checks.
- `src/nanolab/config/scenario.py`, `src/nanolab/cli/product.py`: option validation, file-relative loading and run-directory plumbing.
- `src/nanolab/plans/validate.py`, `src/nanolab/tasks/platform.py`, `src/nanolab/tasks/validate.py`: graph wiring and optional distribution binding.
- `src/nanolab/tasks/compose.py`, `src/nanolab/tasks/function.py`: dynamic image acquisition and task-compatible registration.
- `src/nanolab/tui/app.py`: preserve scenario/profile resolution and run directory on TUI paths.
- `scenarios-v2/deployment-lifecycle-container.yaml`, `recipes/README.md`: select/document the recipe.

## Task 1: Isolated inputs and retained evidence

**Files:** Create `src/nanolab/workspace/recipe.py` and `tests/workspace/test_recipe.py`.

**Interfaces:**
- `RecipeRun(source_dir: Path, recipe: Path, output_dir: Path, tag: str)` is an immutable record of resolved run inputs.
- `prepare_recipe_run(source: Path, recipe: Path, run_dir: Path, tag: str) -> RecipeRun` performs staging only when executed, not during plan construction.

- [ ] Write `test_prepare_recipe_run_preserves_tracked_changes`: a temporary Git source with tracked edits, a deleted file, an executable and spaces in paths produces matching source bytes/modes and revision/dirty metadata; original Git status and files remain unchanged.
- [ ] Write `test_prepare_recipe_run_retains_profile_and_rejects_unsafe_reuse`: exact YAML bytes and their hash are retained; existing incompatible run inputs fail rather than being overwritten. A symlink escaping the staged source must not cause writes to the original.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/workspace/test_recipe.py --no-cov` from repo root; confirm failures for missing behavior.
- [ ] Implement staging using a separate Git clone without hardlinks, detached at the captured revision, plus the tracked binary working-tree diff. Preserve source provenance separately. Detect source changes during capture; reject unsupported submodule/untracked build inputs with an explicit error instead of silently building different inputs. Do not copy caches or credentials. Profile, source and output use separate subdirectories in the run directory.
- [ ] Rerun the tests; require PASS. Verify retry uses the same tag and immutable inputs. Keep artifacts for inspection after failure.

## Task 2: Alternative Gradle tasks and distribution contract

**Files:** Create `src/nanolab/tasks/recipe.py` and `tests/tasks/test_recipe.py`.

**Interfaces:**
- `RecipeImage(reference: str, id: str, status: str, digest: str | None)`.
- `RecipeComponent(kind: str, name: str, sdk: str, mode: str | None, image: RecipeImage, variant: str | None, optimization: str | None)`.
- `RecipeDistribution(report: Path, recipe_sha256: str, tag: str, source: dict[str, object] | None, modules: tuple[str, ...], components: tuple[RecipeComponent, ...])`; `control_plane()` and `function(name: str, sdk: str)` return the uniquely matching component or raise.
- `read_distribution(report: Path, *, recipe: Path, tag: str, published: bool) -> RecipeDistribution` parses the actual schema emitted by NanoFaaS; the boolean is internal report policy, not a public task mode.
- `AssembleRecipeTask` and `PublishRecipeTask` implement `Task[RecipeDistribution]`; constructor inputs: `run: RecipeRun`, `executor: CommandTaskExecutor`. Both run on host and return `TaskOutcome[RecipeDistribution]`.
- `recipe_distribution_resource(*, source: Path, recipe: Path, run_dir: Path, tag: str, executor: CommandTaskExecutor, requires: tuple[Resource[Any], ...]) -> Resource[RecipeDistribution]` prepares inputs and runs `PublishRecipeTask` for the validation workflow; retains evidence on release.

- [ ] Write `test_recipe_tasks_run_distinct_gradle_targets`: recording executor writes realistic report fixtures; assert one Gradle command, exact selected target, absolute recipe/output arguments, run tag and staged cwd. No separate assembly precedes publish and no legacy module flags are sent.
- [ ] Write parametrized report tests: assembly accepts `built` without digest; publish requires `published` plus digest; reject missing ID, unsupported schema/platforms, wrong hash/tag, duplicate components, malformed JSON and partial/failed publication. Obtain fixture field names from the current NanoFaaS report writer, not an invented schema.
- [ ] Write `test_failed_command_never_returns_stale_distribution`: a pre-existing valid report plus a failing command still fails. Assert report evidence remains available and no consumer executes.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/tasks/test_recipe.py --no-cov`; verify expected failures.
- [ ] Implement both task classes using existing Gradle/command execution, one shared report reader and small shared helpers. Require registry-backed profiles for tag overrides; unsupported combinations fail clearly. Preserve partial reports but return a value only after command and contract succeed.
- [ ] Rerun tests; require PASS. Check Gradle invocation with paths containing spaces remains argv-based.

## Task 3: Scenario selection and pure planning

**Files:** Modify `src/nanolab/config/scenario.py`, `src/nanolab/cli/product.py`, `src/nanolab/plans/validate.py`, `src/nanolab/tui/app.py`; add `tests/config/test_recipe.py`, `tests/plans/test_recipe_validate.py`.

**Interfaces:**
- `ScenarioConfig.recipe_profile: Path | None`, YAML alias `recipeProfile`.
- File loading resolves relative paths against the scenario's parent and persists the absolute path through model copies/TUI serialization; direct programmatic configs must supply an absolute path.
- `build_validate_plan(..., run_dir: Path | None = None)` accepts the existing CLI/TUI run directory. Allocate the tag once per run, not each time the graph is rendered. Preview may use a deterministic placeholder and must not allocate/stage a real run.

- [ ] Write tests for resolution from another cwd and preserved resolution through TUI/config round trips. Reject missing profile, unsupported workflow/backend/provider, buildpack, explicit image/runtime/variant overrides, async/envelope/recovery/retry-burst modes not supported by this recipe path.
- [ ] Write `test_recipe_plan_is_read_only`: compile/preview with guarded executors and source snapshot; assert no commands, directories or source changes. Existing scenario without `recipeProfile` still compiles through its old path.
- [ ] Run the two new test files with `.venv/bin/pytest -c packages/nanolab/pyproject.toml ... --no-cov`; confirm failures before editing behavior.
- [ ] Implement config validation and loader/run-context plumbing at the existing file loading boundary in `cli/product.py`; cover any direct TUI loading path. Do not duplicate NanoFaaS recipe validation. Validate the initial recipe's registry matches the local registry resource and selected components match catalog `(family, sdk)` identities; map catalog `exec` to recipe `bash` explicitly if encountered, never parse image names.
- [ ] Rerun tests and `tests/config/test_scenario.py`, `tests/plans/test_validate.py`; require PASS.

## Task 4: Consume the report and prove runtime identity

**Files:** Modify `src/nanolab/tasks/compose.py`, `src/nanolab/tasks/platform.py`, `src/nanolab/tasks/function.py`, `src/nanolab/tasks/validate.py`, `src/nanolab/plans/validate.py`; create `src/nanolab/tasks/recipe_validation.py`, `tests/tasks/test_recipe_validation.py`.

**Interfaces:**
- Add `RecipeBinding(distribution: Resource[RecipeDistribution], functions: dict[str, tuple[str, str]])` in `tasks/recipe.py`, mapping registration names to report `(name, sdk)` keys.
- `PlatformRequest.recipe: RecipeBinding | None = None`; recipe path sets `build_images=False` and rejects attempts to combine it with legacy builds.
- `RecipeFunctionRegisterTask` resolves the binding via `TaskInputs`, constructs the manifest with the report reference and delegates to the existing `HttpFunctionRegisterTask`. Broaden `function_resource`'s `register` annotation to `Task[TaskResult]`; retain its compensation and static callers.
- `recipe_compose_resource(project: DockerComposeProject, *, distribution: Resource[RecipeDistribution], executor: CommandTaskExecutor, cwd: Path, requires: tuple[Resource[Any], ...]) -> Resource[DockerComposeProject]` resolves the image at acquire time, sets its environment and issues `up --no-build`; cleanup uses the same resolved project/environment even on partial failure.
- `RecipeIdentityCheckTask` verifies metadata for the control plane; `RecipeImageCheckTask` verifies the control plane/function container `.Image`. Both save responses under the run directory and use the distribution resource through real dependency edges.

- [ ] Write a recording-executor workflow test with report image references different from catalog defaults. Assert registration and Compose use those references, Compose explicitly disables builds, exactly one `publishRecipe` is scheduled and no legacy image builds/pushes run.
- [ ] Write identity tests: expected variant `recipe-v2-jvm`, build type `jvm`, optimization `c2`, exact modules `build-metadata` and `container-deployment-provider`; wrong metadata or changed container image ID fails even if tags match. Compare reported source revision/dirty fields when available.
- [ ] Write acquisition/failure tests proving registry → recipe → Compose → registration ordering, no deploy after invalid report, and function/Compose compensation after readiness, image or invocation failure. Resolve the Compose container ID with `compose ps`; reuse managed-container naming from existing resource checks.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/tasks/test_recipe_validation.py packages/nanolab/tests/plans/test_recipe_validate.py --no-cov`; confirm expected failures.
- [ ] Implement the optional binding path. Reuse existing invoke and resource checks. Attach image checks after registration/readiness and all consumers to their report dependency. Keep original report, runtime JSON and inspected IDs as evidence; do not hide a nested workflow in a task.
- [ ] Rerun tests plus `tests/plans/test_validate.py` and `tests/tasks/migrated/test_validate_workflow.py`; require PASS.

## Task 5: Migrate the profile consumer and run local validation

**Files:** Modify `scenarios-v2/deployment-lifecycle-container.yaml`, `recipes/README.md`; add verification results to this plan.

- [ ] Add `recipeProfile: ../recipes/validate-container-jvm.yaml` to the chosen scenario and document the two alternative tasks and evidence paths. The profile already exists and passed NanoFaaS `validateRecipe`; do not infer E2E success from that preview.
- [ ] Run full NanoLab tests with `NANOFAAS_ROOT=/home/michele/Documenti/nanofaas uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests`. Report any failures by name, including checkout/version mismatches.
- [ ] Run `uv run --frozen ruff check packages`, `uv run --frozen --all-packages --all-groups basedpyright --project packages/nanolab`, and `uv run --frozen --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache`; require success for the changed package, report unrelated failures explicitly.
- [ ] Preview the migrated scenario, then run `NANOFAAS_ROOT=/home/michele/Documenti/nanofaas ./nanolab.sh run packages/nanolab/scenarios-v2/deployment-lifecycle-container.yaml` from NanoLab with local Docker available. Check existing service/port occupancy before acquiring the fixed local resources; do not destroy unrelated workloads to make the run pass.
- [ ] Verify publication digests, runtime metadata, both container IDs versus report IDs, invocation results and resource assertions. Confirm cleanup and retained evidence; compare NanoFaaS status/content to the pre-run snapshot. If infrastructure prevents execution, report the precise blocked step and do not mark E2E complete.
- [ ] Review the final diff against the spec. Before any commit, run GitNexus `detect-changes --scope all --repo nanolab`; partial/truncated output needs resolution. Commit only task files, preserving unrelated work. Record the actual tests and E2E outcome here.

## Execution evidence (2026-09-28)

- `validateRecipe` accepted the checked-in v2 profile before implementation.
- `nanolab plan` lists registry → `PublishRecipeTask` → Compose → metadata/image checks → registration/invocation/resource checks → reverse cleanup.
- Local E2E run: `/tmp/nanolab-recipe-v2-e2e-20260928`; all 13 acquire/check/release steps passed. The report records two `published` images with verified digests. Saved runtime image IDs match both report IDs; metadata reports JVM, variant `recipe-v2-jvm`, optimization `c2` and the exact two selected modules.
- Separate `assembleRecipe` run on the staged source succeeded with two `built` images, local IDs and no publication digests.
- `basedpyright`, `ruff check packages` and import-linter passed. The extended suite passed 2770 tests with 7 deselected: five existing enormous cpuset preflight cases and two tests whose failures reproduce on the unchanged NanoLab `main` against the current dirty NanoFaaS checkout (release clean-tree requirement; renamed `ExecutionStoreProperties` bean). The first sandboxed full run also exposed a local-socket permission error; rerunning with the required permission removed it.
- NanoFaaS `NANOFAAS_ROOT` status after E2E matches the pre-run status; Gradle built in the retained staged source.
