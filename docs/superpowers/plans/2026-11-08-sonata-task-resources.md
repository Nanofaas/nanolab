# Sonata task resources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Sonata `Resource` a pair of `Task`s (callable resources removed) and add `compose()`, a resource made of an ordered sequence of task resources; release it as Sonata 0.7.0.

**Architecture:** The compiler already wraps acquire/release in engine-owned `ResourceOp` units run by the normal runner; `ResourceOp` now delegates to the resource's own acquire/release `Task` instead of calling callables, and both run with a step scope so a `Steps`-shaped acquire or release is journalled per step. The release task reads the acquired value through `inputs.upstream()`. `compose()` builds a `Resource[Members]` whose acquire/release tasks run the members' tasks as steps.

**Tech Stack:** Python 3.12, uv workspace, pytest, ruff, basedpyright, import-linter. Repository: `/Users/micheleciavotta/Downloads/sonata` (remote `miciav/sonata`).

**Spec:** `/Users/micheleciavotta/Downloads/nanolab/docs/superpowers/specs/2026-11-08-nanofaas-workbench-design.md`, Part 1. Also read the Sonata repo's `CLAUDE.md` and `docs/specs/2026-07-28-composite-task-design.md` (how `Steps` and step scopes work) before Task 1.

## Global Constraints

- `sonata-engine` keeps **zero runtime dependencies** and imports nothing product-specific.
- Callable resources are removed: no `Resource(acquire=<callable>)` path, no adapter in the public API.
- No code reading journals written by 0.6.x; every journal feature (resume, `keep`, `revive`, `release_retained`) must keep working for task resources.
- Release is one breaking version, **0.7.0**, for both distributions; four version sites move together (two pyprojects, `src/sonata_engine/__init__.py` `__version__`, the `sonata-engine==` pin in `packages/sonata-tasks/pyproject.toml`), then `uv lock`.
- Coverage gate `fail_under = 90` (engine) unchanged; ruff 88 cols; basedpyright standard; `reportImplicitOverride` on.
- Kept unchanged: `requires`, `always_release`, `revive`, identity semantics (`eq=False`), acquire spliced before first consumer, release after last, reverse release on failure.

## Review Focus

1. A member's release fails during a composite release → every other member is still released, collect failures and release failures are raised together (`Task 2: test_compose_release_attempts_every_member`).
2. `KeyboardInterrupt` lands in the 2nd member's acquire → the 1st member is released and the `KeyboardInterrupt` (not a wrapper) propagates (`Task 2: test_compose_acquire_unwinds_on_interrupt`).
3. A kept composite released from a later process via `release_retained` → its members are revived and released in reverse (`Task 2: test_release_retained_composite`).
4. A `Steps` used as a release task in `release_retained` (no journal, no runner) → it runs instead of raising `StepScopeUnavailableError` (`Task 1: test_release_retained_runs_steps_release`).
5. A member also required directly by a consumer → `compile()` raises before anything runs (`Task 2: test_compile_rejects_directly_required_member`).

---

### Task 1: Resources made of tasks

**Files:**
- Modify: `src/sonata_engine/core/resource_task.py` (fields, `ResourceOp`)
- Modify: `src/sonata_engine/core/workflow.py:537-593` (`_release`: step scope + upstream), `:729-734` (drop `acquire_idempotent`)
- Modify: `src/sonata_engine/core/step_scope.py` (add `rebased`)
- Modify: `src/sonata_engine/retention.py:87-100` (run the release task)
- Create: `tests/support.py` (test-only task helpers)
- Modify: every engine test and fixture building `Resource(...)`: `tests/core/test_resource_cleanup.py`, `test_compiled.py`, `test_compile_selection.py`, `test_run_selection.py`, `test_workflow.py`, `tests/test_journal.py`, `test_resume.py`, `test_retention.py`, `tests/typecheck/resource_contracts.py`, `task_contracts.py`, `examples/demo_workflow.py`
- Test: `tests/core/test_task_resources.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True, slots=True, eq=False)
  class Resource[T]:
      title: str
      acquire: Task[T]                 # its `idempotent` replaces acquire_idempotent
      release: Task[None]              # reads the value via inputs.upstream()
      requires: tuple[Resource[Any], ...] = ()
      always_release: bool = False
      revive: Callable[[Any], T] | None = None
  ```
  `ResourceOp.run` delegates to `resource.acquire.run(inputs)` / `resource.release.run(inputs)`; `ResourceOp.idempotent` is the acquire task's `idempotent` for acquire units, `False` for release; `ResourceOp._fingerprint_payload()` returns `(qualname of the delegated task's class, delegated task's _fingerprint_payload())`.
  Release inputs: `state.inputs_for((resource, *resource.requires))` with `_upstream=<acquired value>` and a `_StepScope(prefix=<release task_id>, jrnl=jrnl, resume=False)`.
  `_StepScopeProtocol.rebased(self, inputs: TaskInputs) -> _StepScopeProtocol` — same prefix/journal/resume, new base inputs (used by Task 2).
  `retention.release_retained` runs `resource.release.run(...)` with `_upstream=<revived value>` and a `_StepScope(prefix=f"retained/{slug(title)}", jrnl=None, resume=False)`.
- Test helper (tests only, `tests/support.py`): `CallTask[T](title: str, fn: Callable[[TaskInputs], T], *, idempotent: bool = False)` returning `TaskOutcome(value=fn(inputs))`, and `task_resource(title, acquire_fn, release_fn, **kw) -> Resource` where `release_fn(inputs, value)` gets `value = inputs.upstream()`. Used to migrate the existing tests mechanically.

- [ ] **Step 0: Update the checkout and branch**

Run: `cd ~/Downloads/sonata && git pull --ff-only && git switch -c feat/task-resources`
Expected: fast-forward to `e3fe5b8` (0.6.20), new branch.

- [ ] **Step 1: Write the failing tests** in `tests/core/test_task_resources.py`:
  - `test_release_task_reads_acquired_value_as_upstream`: acquire task returns `"vm-1"`; release records `inputs.upstream()`; after `Workflow.run()` the recorded list is `["vm-1"]`.
  - `test_acquire_idempotence_comes_from_the_task`: acquire task with `idempotent=True` fails on attempt 1 under a journal; `resume=True` re-runs it (decision `run`), and with `idempotent=False` resume raises `AmbiguousTaskStateError` — the same matrix the old `acquire_idempotent` test asserted.
  - `test_steps_acquire_is_journalled_per_step`: acquire is `Steps(title="Acquire db", steps=(a, b))`; the journal contains records for `001.acquire-db/a` and `001.acquire-db/b`.
  - `test_steps_release_runs_under_runner`: release is a `Steps`; run succeeds and records `NNN.release-db/<step>` entries.
  - `test_release_retained_runs_steps_release`: `keep=True` run with a journal, then `release_retained({"Acquire db": res}, cfg)` where `res.release` is a `Steps`; returns `("Acquire db",)` and both steps ran.
  - `test_fingerprint_changes_with_acquire_task_payload`: two workflows differing only in the acquire task's `_fingerprint_payload()` have different `compile().fingerprint`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -c pyproject.toml tests/core/test_task_resources.py -v`
Expected: FAIL (`Resource` rejects tasks / `upstream()` raises `NoUpstreamValueError`).

- [ ] **Step 3: Implement** the `Resource`/`ResourceOp`/`_release`/`rebased`/`release_retained` changes in Interfaces. Delete `acquire_idempotent` everywhere (field, compiler, docstrings).

- [ ] **Step 4: Migrate the existing engine tests, fixtures and example** to `tests/support.py` helpers (examples/ and `tests/typecheck/` get small real `Task` classes instead, since they are documentation). No assertion changes other than `acquire_idempotent=` → `idempotent=` on the acquire task.

- [ ] **Step 5: Run the engine suite and checks**

Run: `uv run pytest -c pyproject.toml tests && uv run ruff check src tests && uv run basedpyright --project .`
Expected: all pass, coverage ≥ 90.

- [ ] **Step 6: Commit**

```bash
git add -A src tests examples
git commit -m "feat!: resources are pairs of tasks; callable resources removed"
```

---

### Task 2: `compose()` — a resource made of task resources

**Files:**
- Create: `src/sonata_engine/core/compose.py`
- Modify: `src/sonata_engine/core/__init__.py`, `src/sonata_engine/__init__.py` (export `compose`, `Members`, `CompositeMemberRequiredError`)
- Modify: `src/sonata_engine/errors.py` (add `CompositeMemberRequiredError(ValueError)`)
- Modify: `src/sonata_engine/core/workflow.py` `_merge_resources` (member-required check)
- Test: `tests/core/test_compose.py`

**Interfaces:**
- Consumes: Task 1's `Resource`, `_StepScopeProtocol.rebased`, release upstream contract.
- Produces:
  ```python
  class Members(dict[str, object]):
      def of[T](self, resource: Resource[T]) -> T: ...   # KeyError names the title

  def compose(
      title: str,
      members: tuple[Resource[Any], ...],
      *,
      collect: tuple[Task[Any], ...] = (),
  ) -> Resource[Members]: ...
  ```
  Rules fixed here:
  - `members` non-empty, titles unique (by slug) → else `ValueError`.
  - `requires` = members' `requires` in declaration order, de-duplicated by identity, excluding the members themselves.
  - `always_release` = `any(m.always_release for m in members)`.
  - `revive(raw: dict) -> Members`: applies each member's `revive` to its entry (identity when `None`).
  - Acquire task (title = `title`): for each member in order, `scope.rebased(<inputs: composite requires + earlier members' values>).run_step(member.acquire, f"acquire-{slug(member.title)}", upstream=<previous member value or no upstream for the first>)`. On any `BaseException`, release the already-acquired members in reverse (best effort, errors added as notes) and re-raise the original exception unchanged.
  - Release task (title = `f"Release {title.removeprefix('Acquire ')}"`): `members = inputs.upstream()`; run each `collect` task as step `collect-<slug>` with upstream `members`; then each member's release in reverse as step `release-<slug>` with upstream = that member's value and inputs exposing all member values. Every step is attempted; failures are collected and raised at the end (one → itself, several → `RuntimeError` listing them, critical `BaseException` wins as in `Workflow._raise_final`).
  - `_fingerprint_payload` of both tasks covers every member's acquire/release payloads and the collect tasks.
  - Compile: if a resource reachable as a composite member is also in some consumer's `requires` (or another resource's `requires` outside the composite), raise `CompositeMemberRequiredError` naming both titles. The compiler identifies composites by their acquire task's type (`_AcquireMembers` in `compose.py`), exposing `members`.

- [ ] **Step 1: Write the failing tests** in `tests/core/test_compose.py`:
  - `test_compose_acquires_in_order_releases_in_reverse`: members a, b, c; calls == `["acquire.a","acquire.b","acquire.c", "consumer", "release.c","release.b","release.a"]`.
  - `test_consumer_reads_member_values`: consumer gets `inputs.resource(platform).of(b) == "B"`.
  - `test_later_member_reads_earlier_member`: b's `requires=(a,)`, b's acquire reads `inputs.resource(a)`; composite `requires` is `()`.
  - `test_compose_acquire_failure_releases_acquired_members`: b's acquire raises `RuntimeError("b")`; calls end with `"release.a"`, c never acquired, raised error is that `RuntimeError`.
  - `test_compose_acquire_unwinds_on_interrupt`: same with `KeyboardInterrupt`; `pytest.raises(KeyboardInterrupt)`, `"release.a"` recorded.
  - `test_compose_release_attempts_every_member`: collect task and b's release both raise; a and c still released; raised `RuntimeError` message contains both errors.
  - `test_collect_runs_before_member_releases`: collect sees `inputs.upstream().of(a)`; recorded before any release.
  - `test_compose_is_always_release_when_a_member_is`: `keep=True`; a member `always_release=True` → the whole composite released at end of run.
  - `test_release_retained_composite`: `keep=True` with journal, members' values dataclasses with `revive`; `release_retained({"Acquire platform": platform}, cfg)` releases c, b, a with revived values.
  - `test_compose_steps_are_journalled`: journal contains `NNN.acquire-platform/acquire-a` … and `…/release-a`.
  - `test_compile_rejects_directly_required_member`: workflow requires both `platform` and `a` → `CompositeMemberRequiredError` at `compile()`.
  - `test_compose_rejects_empty_and_duplicate_members`: `ValueError` for `()` and for two members with the same title.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -c pyproject.toml tests/core/test_compose.py -v`
Expected: FAIL with `ImportError: cannot import name 'compose'`.

- [ ] **Step 3: Implement `compose.py`, the error, the compile check and the exports** per Interfaces.

- [ ] **Step 4: Run the engine suite and checks**

Run: `uv run pytest -c pyproject.toml tests && uv run ruff check src tests && uv run basedpyright --project .`
Expected: all pass, coverage ≥ 90.

- [ ] **Step 5: Commit**

```bash
git add src tests
git commit -m "feat: compose resources from an ordered sequence of task resources"
```

---

### Task 3: Convert sonata-tasks

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/compensation.py` (`compensated_resource`), `archive.py:312`, `buildx.py:246`, `kubectl.py:215,297`, `minikube.py:157`, `process.py:164`, `registry.py:188`, `registry_tunnel.py:120`, `compose.py` (its `compensated_resource` use)
- Modify: `packages/sonata-tasks/README.md` (resource sections, the `builder.acquire(inputs)` example at ~L191)
- Modify: tests under `packages/sonata-tasks/tests/` that build or call resources (`test_compose.py`, `test_helm.py`, `test_kubernetes_resources.py`, and any calling `.acquire(`/`.release(` directly)

**Interfaces:**
- Consumes: Task 1's `Resource` contract.
- Produces: `compensated_resource(*, title, acquire: Task[T], compensate: Task[Any], release: Task[None] | None = None, requires=(), always_release=False, revive=None) -> Resource[T]` — on acquire failure runs `compensate` best-effort (via the existing `best_effort`) and re-raises; release runs `release` if given, else `compensate`. Every resource factory keeps its current signature and behaviour; its acquire/release closures become small private `Task` classes in the same module (titles: the resource's title for acquire, `release_title` for release). `process.py` keeps its acquire-local stop on readiness failure.

- [ ] **Step 1: Update the tests that call `resource.acquire(inputs)` / `resource.release(inputs, v)` directly** to run through a `Workflow` or call `resource.acquire.run(inputs).value` / `resource.release.run(replace(inputs, _upstream=v))`; run them to see them fail.

Run: `uv run pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q`
Expected: FAIL in the resource tests.

- [ ] **Step 2: Convert `compensated_resource` and the nine factories.**

- [ ] **Step 3: Run the catalogue checks**

Run: `uv run pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests && uv run ruff check --config packages/sonata-tasks/pyproject.toml packages/sonata-tasks && uv run basedpyright --project packages/sonata-tasks && uv run lint-imports --config packages/sonata-tasks/.importlinter --no-cache`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add packages/sonata-tasks
git commit -m "feat!: sonata-tasks resources are task resources"
```

---

### Task 4: Docs and 0.7.0

**Files:**
- Modify: `README.md` (Resource section: task pairs, upstream value, `compose`, `Members.of`, migration note "0.7.0 removes callable resources; drain kept resources and interrupted runs with 0.6.x before upgrading")
- Modify: `CLAUDE.md` (architecture paragraph: resources are task pairs; `compose`)
- Create: `docs/specs/2026-11-08-task-resources-design.md` (Part 1 of the spec plus the "Settled while planning" calls, copied, so Sonata's own design record is complete)
- Modify: `pyproject.toml`, `packages/sonata-tasks/pyproject.toml` (version `0.7.0`, pin `sonata-engine==0.7.0`), `src/sonata_engine/__init__.py` (`__version__ = "0.7.0"`), `uv.lock`

- [ ] **Step 0: Check for other importers of the removed API**

Run: `gh search code "sonata_engine" --owner miciav --owner Nanofaas --json repository --jq '.[].repository.nameWithOwner' | sort -u`
Expected: only `miciav/sonata` and `Nanofaas/nanolab` (if another repo appears, stop and ask before releasing).

- [ ] **Step 1: Bump the four version sites and lock**

Run: `uv lock && uv run pytest -c pyproject.toml tests -k dunder_version`
Expected: `test_dunder_version_matches_the_pyproject` PASS.

- [ ] **Step 2: Write the docs.**

- [ ] **Step 3: Run everything CI runs**

Run: `uv run pre-commit run --all-files`
Expected: all hooks pass.

- [ ] **Step 4: Commit and open the PR**

```bash
git add -A
git commit -m "chore: release 0.7.0 — task resources and compose"
git push -u origin feat/task-resources
gh pr create --title "Task resources and compose (0.7.0)" --body "..."
```
Publishing to PyPI (tag → release workflow, engine before tasks) is Michele's step after merge.

---

## After this plan

Next plan (nanolab, before the workbench): drain kept resources/interrupted runs with 0.6.20, bump the three pins to 0.7.0, convert nanolab's ~47 callable resources in 24 files to task resources, keep `grep -c 'git+' uv.lock` at 0, run the full suite and the checkout contracts. Then the workbench plan (Part 2) and the platform-resource plan (Part 3).
