# nanolab on Sonata 0.7.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move nanolab from Sonata 0.6.20 to 0.7.0, where every `Resource` is a pair of `Task`s, with no behaviour change.

**Architecture:** One small nanolab-private module, `nanolab/tasks/resource_tasks.py`, turns nanolab's existing acquire/release closures into tasks (`call`, `release_call`, `resource_of`) and runs a resource outside a workflow (`acquire_now`, `release_now`). Every call site is then converted mechanically by area. Real per-component tasks come later, with the workbench (spec Part 3); this plan only keeps nanolab working on 0.7.0.

**Tech Stack:** Python 3.12, uv workspace, Sonata 0.7.0 (`sonata-engine`, `sonata-tasks`), pytest, ruff, basedpyright, import-linter.

**Spec:** `docs/superpowers/specs/2026-11-08-nanofaas-workbench-design.md` (Part 1 "Journal compatibility", Part 3 "The current callable resources become task resources"). Sonata 0.7.0 API: `~/Downloads/sonata/README.md` "Resources" and `docs/specs/2026-11-08-task-resources-design.md` ("As built").

## Preconditions (operator, before Task 1)

1. `sonata-engine==0.7.0` and `sonata-tasks==0.7.0` are on PyPI (`uv pip index versions sonata-engine` lists 0.7.0). Without it `uv lock` fails; do not work around it with a git or path source.
2. Drain with nanolab on 0.6.20 (today's `main`): run `nanolab teardown` for every environment that has a release journal with retained resources, run the soak teardown for any soak run kept with `keep`, and finish or discard interrupted runs under `$NANOLAB_WORKSPACE/runs/`. 0.7.0 cannot resume 0.6.x journals.

## Global Constraints

- Pins move together to `0.7.0` in three places: workspace `pyproject.toml` (`sonata-engine==`), `packages/nanolab/pyproject.toml` (`sonata-engine==`, `sonata-tasks[shell,prometheus,multipass,azure,proxmox]==`); then `uv lock`. `grep -c 'git+' uv.lock` stays `0`.
- No behaviour change: same titles, same `requires`, `always_release`, `revive`, same order of side effects, same error messages.
- nanolab must never write to the nanoFaaS checkout (`NANOFAAS_ROOT`).
- Conversion rules, applied everywhere:
  - `Resource(title=t, acquire=f, release=g, **kw)` → `resource_of(t, f, g, **kw)`.
  - `acquire_idempotent=x` → `idempotent=x`; reading `r.acquire_idempotent` → `r.acquire.idempotent`.
  - `compensated_resource(acquire=f, compensate=h, release=g, …)` → `compensated_resource(acquire=call(t, f), compensate=call(f"Compensate {t}", h), release=release_call(t, g) if g else None, …)` with `t` the resource title.
  - `r.acquire(inputs)` → `acquire_now(r, inputs)`; `r.release(inputs, v)` → `release_now(r, inputs, v)`; `stack.callback(r.release, inputs, v)` → `stack.callback(release_now, r, inputs, v)`.
- Each task after Task 1 runs the focused tests of its area; the full suite is green only at Task 5 (the pin bump breaks unconverted areas). Each task still commits.
- Commands (from the repo root):
  - tests: `NANOFAAS_ROOT=<CI-pinned checkout> uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests`
  - `uv run --frozen ruff check packages`, `uv run --frozen --all-packages --all-groups basedpyright --project packages/nanolab`, `uv run --frozen --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache`
  - The CI pin is the `ref:` in `.github/actions/setup-workspace/action.yml`; never stash or edit the operator's nanoFaaS checkout — check the pinned revision out separately if needed.

## Review Focus

1. A resource whose release reads its acquired value: after conversion the value still arrives (via `inputs.upstream()`), including `None` values (`tests/tasks/test_resource_tasks.py::test_release_now_passes_none`).
2. Wrappers that re-wrap another resource (`tasks/soak/retention.py`, `tasks/soak/owned_functions.py`, `one_shot/infrastructure.py`) keep the wrapped resource's `requires`, `always_release`, `revive` and idempotence (Task 2 / Task 3 tests named there).
3. `release_retained` teardown paths (`cli/product.py`, `tasks/soak/teardown.py`) still release every retained title (Task 3, Task 5 tests).
4. Resources driven by hand inside an `ExitStack` (`tasks/one_shot/capacity.py`, `qualification_load.py`, `tasks/recipes/builder.py`) still release on failure (Task 2 / Task 4 tests).
5. `compensated_resource` call sites still compensate on a failed acquire (existing tests in each area keep asserting it).

---

### Task 1: Pins and the resource-task helper

**Files:**
- Modify: `pyproject.toml`, `packages/nanolab/pyproject.toml`, `uv.lock`, `CLAUDE.md` ("Pin discipline" block: `0.6.20` → `0.7.0`)
- Create: `packages/nanolab/src/nanolab/tasks/resource_tasks.py`
- Test: `packages/nanolab/tests/tasks/test_resource_tasks.py`

**Interfaces:**
- Produces (all in `nanolab.tasks.resource_tasks`):
  ```python
  def call[T](title: str, fn: Callable[[TaskInputs], T], *, idempotent: bool = False) -> Task[T]
  def release_call[T](title: str, fn: Callable[[TaskInputs, T], object]) -> Task[None]
  def resource_of[T](
      title: str,
      acquire: Callable[[TaskInputs], T],
      release: Callable[[TaskInputs, T], object],
      *,
      requires: tuple[Resource[Any], ...] = (),
      always_release: bool = False,
      revive: Callable[[Any], T] | None = None,
      idempotent: bool = False,
  ) -> Resource[T]
  def acquire_now[T](resource: Resource[T], inputs: TaskInputs) -> T
  def release_now[T](resource: Resource[T], inputs: TaskInputs, value: T) -> None
  ```
  `call` runs `fn(inputs)` and returns `TaskOutcome(value=…)`; `release_call` runs `fn(inputs, inputs.upstream())`; `resource_of` titles the release task `"Release " + title.removeprefix("Acquire ")`; `acquire_now` is `resource.acquire.run(inputs).value`; `release_now` runs the release task with `dataclasses.replace(inputs, _upstream=value)` — mark it `# ponytail: Sonata has no public "release by hand"; one place reads the private _upstream field`.

- [ ] **Step 1: Bump the three pins, `uv lock`, update CLAUDE.md; check** `grep -c 'git+' uv.lock` → `0`.
- [ ] **Step 2: Write the failing tests** in `test_resource_tasks.py`: `test_resource_of_runs_in_a_workflow` (acquire value reaches the release fn; calls order acquire → consumer → release), `test_resource_of_keeps_options` (requires, always_release, revive, `acquire.idempotent` as passed), `test_release_title_follows_the_engine` (`resource_of("Acquire vm", …).release_title == "Release vm"`), `test_acquire_now_and_release_now` (value round-trip), `test_release_now_passes_none`, `test_call_and_release_call_titles`.
- [ ] **Step 3: Run them** — `… pytest … packages/nanolab/tests/tasks/test_resource_tasks.py -v` → FAIL (module missing).
- [ ] **Step 4: Implement `resource_tasks.py`.**
- [ ] **Step 5: Run them** → PASS; ruff and basedpyright on the new file clean.
- [ ] **Step 6: Commit** `Use Sonata 0.7.0 and add the resource-task helper`.

### Task 2: One-shot

**Files:** every `Resource(`/`compensated_resource(`/`.acquire(`/`.release(` site under `src/nanolab/one_shot/`, `src/nanolab/tasks/one_shot/` (`clock.py`, `logging.py`, `experiment.py`, `resources.py`, `operator_logging.py`, `capacity.py`, `qualification_load.py`) and `src/nanolab/plans/one_shot_common.py`; their tests under `tests/one_shot/`, `tests/plans/test_one_shot_experiment.py`, and any test of those modules.

**Interfaces:** Consumes Task 1's helper. Produces nothing new.

- [ ] **Step 1: Convert the sources** by the Global Constraints rules. `one_shot/infrastructure.py:415` re-wraps `base`: keep `requires`, `revive=base.revive` and `idempotent=base.acquire.idempotent`.
- [ ] **Step 2: Convert the tests** that build resources or call `.acquire(`/`.release(` to the helper; no assertion changes.
- [ ] **Step 3: Run** `… pytest … packages/nanolab/tests/one_shot packages/nanolab/tests/plans/test_one_shot_experiment.py packages/nanolab/tests/tasks -k "one_shot or capacity or qualification" -q` → PASS.
- [ ] **Step 4: Commit** `One-shot resources on Sonata 0.7.0`.

### Task 3: Soak

**Files:** `src/nanolab/tasks/soak/` (`owned_functions.py`, `teardown.py`, `runtime.py`, `retention.py`, `composition.py`, `preparation.py`) and `src/nanolab/plans/soak.py`; tests under `tests/soak/`, `tests/plans/test_soak.py`.

- [ ] **Step 1: Convert the sources.** `retention.py:433` and `owned_functions.py:199` re-wrap `resource`: keep `requires`, `always_release` and idempotence (`resource.acquire.idempotent`); their inner `resource.acquire(inputs)` becomes `acquire_now(resource, inputs)`. `teardown.py` builds release-only resources for `release_retained`: `resource_of(title, lambda _inputs: None, release_fn)`.
- [ ] **Step 2: Convert the tests.**
- [ ] **Step 3: Run** `… pytest … packages/nanolab/tests/soak packages/nanolab/tests/plans/test_soak.py -q` → PASS.
- [ ] **Step 4: Commit** `Soak resources on Sonata 0.7.0`.

### Task 4: Recipes, validation and platform tasks

**Files:** `src/nanolab/tasks/recipes/` (`multiarch.py`, `workflow.py`, `remote.py`, `builder.py`, `kubernetes.py`, `validation.py`), `src/nanolab/tasks/platform.py`, `containerd_rootless.py`, `function.py`, `cli_artifacts.py`, `module_cost.py`; tests under `tests/tasks/` for them (`recipes/`, `validation/`, `functions/`, `test_compose.py`, `test_offload_tasks.py`, `test_managed_container_inspection.py`, `test_module_cost_build.py`, `loadtest/`).

- [ ] **Step 1: Convert the sources.** `builder.py` drives `builder` by hand: `builder.acquire(inputs)` → `acquire_now(builder, inputs)`, `builder.release(inputs, name)` → `release_now(builder, inputs, name)`.
- [ ] **Step 2: Convert the tests.**
- [ ] **Step 3: Run** `… pytest … packages/nanolab/tests/tasks -q` → PASS.
- [ ] **Step 4: Commit** `Recipe, validation and platform resources on Sonata 0.7.0`.

### Task 5: Release, comparison, remaining plans; full verification

**Files:** `src/nanolab/release/resources.py`, `src/nanolab/comparison/prepare.py`, `src/nanolab/plans/runtime_comparison.py`, `src/nanolab/plans/module_cost.py`, `src/nanolab/cli/product.py` (only if it calls `.acquire`/`.release`), and any remaining site found by `grep -rn "Resource(\|acquire_idempotent\|\.acquire(inputs\|\.release(inputs" packages/nanolab/src packages/nanolab/tests` outside the helper; their tests (`tests/plans/test_release.py`, `tests/release/`, `tests/comparison/`, `tests/cli/`).

- [ ] **Step 1: Convert the sources and tests.**
- [ ] **Step 2: Grep** the line above → only `resource_tasks.py` and Sonata-API uses (`Resource[...]` annotations, `release_retained`) remain.
- [ ] **Step 3: Full verification** — the four commands in Global Constraints with `NANOFAAS_ROOT` at the CI pin, and `uv run pre-commit run --all-files` → all pass, coverage at or above `fail_under`.
- [ ] **Step 4: Commit** `Release, comparison and plan resources on Sonata 0.7.0`.
