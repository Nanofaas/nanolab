# Recipe-backed Container Load Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in the current session. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run `autoscaling-cycle-container.yaml` from one published recipe distribution while preserving its k6 and measurement workflow.

**Architecture:** Reuse the existing staged recipe, distribution reader, `RecipeBinding`, `recipe_compose_resource`, and platform registration path. The load-test plan will create a registry → publication → Compose dependency chain and pass the binding to `add_platform`; the load-test workflow will verify runtime metadata and image IDs before k6.

**Tech Stack:** Python 3.12+, Pydantic, Sonata resources/tasks, Gradle recipes v2, Docker Compose, k6, pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-loadtest-recipes-design.md`

## Global Constraints

- Migrate only `packages/nanolab/scenarios-v2/autoscaling-cycle-container.yaml`; comparison matrices and other load tests keep their current build graph.
- Keep `word-stats-java`, the current k6 cycle, autoscaling, Prometheus queries, report paths, and cleanup behavior.
- Use the local provider, the run-scoped registry, a staged NanoFaaS source, a per-run tag, and `publishRecipe` once. Plan and dry-run have no build or staging side effects.
- Profile modules: `container-deployment-provider`, `autoscaler`, `async-queue`, `build-metadata`; JVM control plane and Java `word-stats`. Use `controlPlane.jvm.args: ["-XX:+UseSerialGC"]`, matching the current control-plane Dockerfile default.
- Retain the staged profile/source, Gradle log, `distribution.json`, runtime image evidence, and load measurements under the run directory. A failed publication blocks deployment and k6.
- Preserve unrelated dirty NanoLab and NanoFaaS files. At execution, create an isolated worktree or explicitly carry the required dirty recipe changes as directed by the worktree skill. Do not commit unrelated files.

## Review Focus

1. A recipe omits or adds a control-plane module: reject its report before Compose starts (Task 2 test).
2. The profile's function does not correspond exactly to `word-stats-java`: reject before registration (Task 2 test).
3. A prior run left a plausible `distribution.json` with a different tag or profile hash: reject it rather than deploy (Task 2 test).
4. Publication fails after the registry starts: retain `gradle.log`, release the registry, and never run Compose or k6 (Task 3 test).
5. A mutable image tag points at different bytes when the container starts: compare runtime image IDs with the distribution before k6 (Task 3 test).

## File map

- `packages/nanolab/src/nanolab/config/scenario.py`: allow a container `loadtest` recipe while keeping incompatible scenario options rejected.
- `packages/nanolab/src/nanolab/tasks/recipe.py`: parameterize publication's required modules without weakening validate callers.
- `packages/nanolab/src/nanolab/plans/loadtest.py`: create recipe resources and binding; disable duplicate builds; preserve load configuration.
- `packages/nanolab/src/nanolab/cli/product.py`: choose a unique default run directory for recipe load tests and preflight the provider before provisioning.
- `packages/nanolab/src/nanolab/tasks/loadtest/__init__.py`: require metadata and image checks before the load composite for recipe runs.
- `packages/nanolab/recipes/loadtest-container-jvm.yaml`, `packages/nanolab/scenarios-v2/autoscaling-cycle-container.yaml`, `.github/workflows/ci.yml`, `packages/nanolab/recipes/README.md`, `docs/recipes-roadmap.md`: reusable profile, consumer, validation, usage and verified status.
- `packages/nanolab/tests/config/test_recipe.py`, `packages/nanolab/tests/tasks/test_recipe.py`, `packages/nanolab/tests/plans/test_loadtest.py`, `packages/nanolab/tests/tasks/migrated/test_loadtest.py`, `packages/nanolab/tests/cli/test_command_surface.py`: focused regressions.

### Task 1: Admit and validate the selected profile

**Files:** modify `packages/nanolab/src/nanolab/config/scenario.py`, `packages/nanolab/tests/config/test_recipe.py`; create `packages/nanolab/recipes/loadtest-container-jvm.yaml`; modify `.github/workflows/ci.yml`.

**Interfaces:** `ScenarioConfig.recipe_profile: Path | None` remains the existing field. `loadtest` accepts it only with `backend: container`, `build: docker`, JVM runtime, local provider at plan preflight, and no conflicting explicit image/variant options. Keep validate rules unchanged.

- [ ] Add `test_container_loadtest_accepts_recipe_profile` and parametrized rejection tests for `containerd`, `k8s`, explicit `controlPlaneImage`, `controlPlaneVariant`, `functionImages`, native runtime and buildpack. Assert the current validate recipe tests still pass.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/config/test_recipe.py --no-cov`; require the new acceptance case to fail first.
- [ ] Adjust the scenario validator for this exact combination. Save `loadtest-container-jvm.yaml` with the four modules and Java function, then add its `validateRecipe` command to CI.
- [ ] Run the same pytest command and `./gradlew validateRecipe -Precipe=<absolute NanoLab profile>` in the NanoFaaS checkout. Require both to pass. Commit only Task 1 files.

### Task 2: Require the selected load-test distribution

**Files:** modify `packages/nanolab/src/nanolab/tasks/recipe.py`, `packages/nanolab/tests/tasks/test_recipe.py`.

**Interfaces:** extend `recipe_distribution_resource(..., required_modules: frozenset[str] = frozenset({'build-metadata', 'container-deployment-provider'}), exact_modules: bool = False) -> Resource[RecipeDistribution]`. Pass the required set into `require_validation_distribution`; when `exact_modules=True`, also require equality with `distribution.modules`. Existing callers keep their defaults. The load-test caller supplies all four profile modules, `exact_modules=True`, and `functions=(('word-stats', 'java'),)`.

- [ ] Add tests for missing `autoscaler`, missing `async-queue`, extra module, wrong/extra function, wrong tag/profile hash, and a successful exact report. Reuse existing report fixtures and check rejection happens before a distribution is returned.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/tasks/test_recipe.py --no-cov`; require the new module-parameter test to fail first.
- [ ] Add the parameters and exact-set check, retaining existing published status and digest checks.
- [ ] Rerun the same tests; require pass. Commit only Task 2 files.

### Task 3: Wire publication and runtime checks into the existing load workflow

**Files:** modify `packages/nanolab/src/nanolab/plans/loadtest.py`, `packages/nanolab/src/nanolab/tasks/loadtest/__init__.py`, `packages/nanolab/tests/plans/test_loadtest.py`, `packages/nanolab/tests/tasks/migrated/test_loadtest.py`, `packages/nanolab/tests/cli/test_command_surface.py`, `packages/nanolab/src/nanolab/cli/product.py`.

**Interfaces:** `build_loadtest_plan(...) -> Workflow` remains public. For the selected recipe scenario, construct `RecipeBinding(distribution, functions, run_dir, project)` and set `PlatformRequest.recipe` with `build_images=False`, `push_function_images=False`, `build_control_plane=False`. Extend `build_loadtest_workflow(...) -> Workflow` to add `RecipeMetadataCheckTask` and `RecipeImageCheckTask` before `load` when `request.recipe` is set. Keep its non-recipe signature/callers compatible.

- [ ] Add a plan test asserting the graph contains exactly one recipe publication, a registry → publication → Compose chain, no legacy component build/push, the same load composite, and no source staging during `plan`. Add a non-recipe graph regression test.
- [ ] Add workflow tests asserting metadata and control-plane/function image checks precede k6, mismatched runtime IDs stop k6, and publication failure leaves diagnostics and releases the registry without starting Compose. Include an unsupported non-local provider preflight test and a test that an omitted `--run-dir` creates a unique recipe run directory instead of reusing `runs/latest`.
- [ ] Run `.venv/bin/pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_loadtest.py packages/nanolab/tests/tasks/migrated/test_loadtest.py packages/nanolab/tests/cli/test_command_surface.py --no-cov`; require the new tests to fail first.
- [ ] Build resources using existing `docker_registry_resource`, `recipe_distribution_resource`, `recipe_compose_resource`, and `RecipeBinding`. Resolve function recipe names/SDKs via `resolve_function_definition`; do not derive them from the image tag. Make the load composite depend on the runtime checks, and retain all existing k6/metrics steps. Add the load-test recipe branch to the CLI run-directory selection and call `require_recipe_environment` before plan/provisioning.
- [ ] Rerun the focused tests and `packages/nanolab/tests/plans/test_runtime_comparison.py`; require pass. Commit only Task 3 files.

### Task 4: Migrate the scenario and verify it end to end

**Files:** modify `packages/nanolab/scenarios-v2/autoscaling-cycle-container.yaml`, `packages/nanolab/recipes/README.md`, `docs/recipes-roadmap.md`.

**Interfaces:** add `recipeProfile: ../recipes/loadtest-container-jvm.yaml` to the existing scenario. Preserve its other YAML fields exactly.

- [ ] Compare `nanolab plan` for the scenario before/after migration: only the build and runtime evidence portion changes; record the k6, autoscaling and metric tasks that stay present.
- [ ] Add the profile selection and usage to the scenario and recipe README. Run `nanolab plan` and `nanolab inspect` for the migrated scenario with `NANOFAAS_ROOT=<checkout>`; require no staging or build side effects.
- [ ] Run `NANOFAAS_ROOT=<checkout> ./nanolab.sh run packages/nanolab/scenarios-v2/autoscaling-cycle-container.yaml --run-dir /tmp/nanolab-loadtest-recipe-<unique-id>` with local Docker. Require publication, registration, k6, autoscaling/metrics, runtime identities and cleanup to pass; retain the report and measurement paths.
- [ ] Run `.venv/bin/ruff check packages/nanolab/src/nanolab/config/scenario.py packages/nanolab/src/nanolab/tasks/recipe.py packages/nanolab/src/nanolab/plans/loadtest.py packages/nanolab/src/nanolab/tasks/loadtest packages/nanolab/tests`, `uv run --frozen --all-packages --all-groups basedpyright --project packages/nanolab`, relevant pytest suites, and `git diff --check`. Address only regressions from this work.
- [ ] Record the verified container sub-slice in the roadmap; leave the whole load-test/runtime-comparison item unchecked until its remaining backends and matrix are complete. Commit only Task 4 files and record evidence paths/results below.

## Execution evidence

Record commands, results, run directory, report path, k6 summary and any unmet verification here during implementation.
