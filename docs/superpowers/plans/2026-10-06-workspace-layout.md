# Workspace Layout Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Address #60 with one distributed resource tree and coherent task/test domains.

**Architecture:** Preserve `application` from #59; package assets own presets,
workspace owns mutable operator inputs/results, CLI resolves names or paths.

**Tech Stack:** Python, pathlib, Typer, pytest, uv, Sonata 0.6.5.

**Spec:** `docs/superpowers/specs/2026-10-06-workspace-layout-design.md`

## Global Constraints

- Keep two packages, existing task behavior and read-only NanoFaaS access.
- Preserve ignored operator configurations; remove only tracked duplicate presets.
- No new dependencies or blanket compatibility modules.
- Existing public compatibility aliases remain until at least 0.7.

## Review Focus

- Explicit missing nested inputs must not accidentally resolve to bundled presets.
- Cwd and configured workspace input precedence must preserve relative YAML references.
- Read-only installed resources must never be the default run/profile destination.
- Moved Dockerfile COPY files and diagnostic locks must exist in the build context.
- Moved tests must retain source markers and patch the implementation module.

### Task 1: Canonical presets and operator inputs

**Files:** `workspace/paths.py`, `cli/catalogue.py`, `cli/product.py`, `tui/app.py`,
`assets/presets`, `tests/workspace/test_paths.py`, installed smoke test.

- [x] Add failing checks for canonical source resources and named CLI inputs.
- [x] Implement `resolve_input_path(path: Path, directory: str) -> Path` with
  explicit/cwd, workspace, preset precedence and file validation.
- [x] Make `discover_tool_root()` return bundled presets in source and wheel;
  integrate CLI resolution before config parsing and release preparation.
- [x] Merge scenarios-v2 into scenarios; delete tracked duplicate resources.
- [x] Verify path precedence, every YAML recipe/policy reference, named commands.

### Task 2: Task and diagnostic domains

**Files:** `tasks/recipes/{workflow,builder,containerd,kubernetes,multiarch,registry,remote,validation}.py`,
`tasks/validation/{workflow,recovery}.py`, `assets/diagnostics`, callers and package-data.

- [x] Move existing bodies unchanged; update imports and monkeypatch targets.
- [x] Merge soak diagnostic assets and validation probes into diagnostics;
  update package-data, Dockerfile COPY, build locks and runtime paths.
- [x] Run recipe/validation/soak regression tests and import/type checks.

### Task 3: Test domains and documentation

**Files:** `tests/tasks/{recipes,validation,loadtest,components,provisioning,infra,vm}`,
README files, CLAUDE.md, CONVENTIONS.md, `.github/workflows/ci.yml`, smoke script.

- [x] Move migrated tests by feature, update shared helpers and path derivations.
- [x] Replace old scenario paths with canonical preset names/paths and document
  operator workspace, writable results and compatibility policy.
- [x] Test installed catalogue, CLI/TUI presets, diagnostics, YAML references
  and run/profile destinations outside checkout.
- [x] Run pure and pinned suites, toolkit, lint/format/types/imports/build smoke.
- [x] Obtain fresh review, fix findings and record final evidence/limitations.

## Verification evidence

- Full pinned-source suite: **3,447 passed** (before the final direct-planner
  regression was added). The final recipe-validation suite, including all six
  backend/output regressions, passes **26 tests**; the final affected validation/comparison
  suite passes **175 tests** after that correction.
- Pure suite without NanoFaaS: **2,837 passed**, 14 checkout-contract modules
  skipped, 381 source-dependent tests deselected.
- Toolkit: **141 passed**, 93.71% coverage (80% threshold).
- Installed wheel: both tests pass, exercising named inputs outside checkout,
  catalogue, CLI/TUI presets, recipe/policy references, diagnostics and writable
  profiles/results. Both package wheels and sdists build offline; lock check passes.
- Four documented inspect commands and four complete plans run outside checkout
  without creating a default output directory. All 35 local links in maintained
  documentation resolve.
- All repository pre-commit hooks pass: lint, format, both type checks, import
  contracts, Bandit, TOML/YAML and file hygiene.
- Fresh review found two material issues: default outputs for direct validation
  plans still used the resource root, and maintained documentation still referenced
  deleted source directories. Both are corrected. Default-output checks were
  watched fail for all three backends, while explicit-output checks already passed;
  all six then passed after the shared operator-workspace fallback was applied.

The nanolab behavior suites above use `--no-cov`. The preexisting package branch
coverage threshold shortfall documented for #59 was not changed or remeasured.
No live infrastructure was executed; NanoFaaS and ignored operator configurations
were not modified. Structural task/test moves add no new distribution package or
blanket compatibility aliases.
