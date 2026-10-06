# Publication Maintainability Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Address #59 with behavior-preserving maintainability and publication checks.

**Architecture:** Common application wiring below CLI and plans; pure shared
metric interpretation; focused extraction of existing responsibilities.

**Tech Stack:** Python 3.12, Sonata 0.6.5, pytest, import-linter, uv.

**Spec:** `docs/superpowers/specs/2026-10-06-publication-maintainability-design.md`

## Global Constraints

- Preserve runtime ownership, cleanup, evidence bounds and policy thresholds.
- NanoFaaS checkout is read-only; Rust implementation belongs to #57.
- Preserve existing public imports with aliases where needed.
- Use existing dependencies; development quality commands stay in dev tooling.

## Review Focus

- CLI compatibility aliases must retain test injection points and object identity.
- Missing/nonfinite observations must not read as a healthy zero.
- Counter resets apply independently to labelled publishers; gauges can fall.
- Tests without a checkout must not read the caller's accidental working directory.
- Wheel checks must import installed resources and write to operator workspace.

### Task 1: Application boundaries

- [x] Add contracts reproducing inverse imports and run them red.
- [x] Move execution/VM/function wiring; keep compatibility aliases.
- [x] Move frozen soak composition and release request data below plans.
- [x] Verify import contracts and affected CLI/plan/runtime tests.

### Task 2: Shared metrics

- [x] Add real-shaped k6 and counter/gauge/missing/nonfinite regression cases; run red.
- [x] Share metric interpretation among comparison, detailed report, summary and release.
- [x] Verify metric/report/release tests.

### Task 3: Responsibility extraction and bootstrap cleanup

- [x] Extract observations/evidence/policy/config rendering using existing behavior tests.
- [x] Reuse `build_ansible_argv`; retain meaningful operation adapters.
- [x] Remove verified unused template/constants and their tautological tests.
- [x] Verify soak, load-test, bootstrap and CLI behavior.

### Task 4: Pure tests and NanoFaaS compatibility

- [x] Reproduce checkout-free test startup failure and unsupported-directory discovery.
- [x] Separate checkout contracts and allow pure suite without source extraction.
- [x] Diagnose unsupported selected functions and document the CI compatibility pin.
- [x] Verify checkout-free suite and pinned full suite; preserve archive isolation.

### Task 5: Metadata, wheel and documentation

- [x] Add READMEs and compatible toolkit requirement; update lock metadata.
- [x] Exercise actual installed wheels, presets, catalogue and writable outputs.
- [x] Correct workspace structure, command naming and behavior-focused conventions.
- [x] Record cloud-extra and operation-wrapper decisions.

### Task 6: Final verification and review

- [x] Run both suites, import contracts, lint, format, types and wheel build/smoke.
- [x] Obtain a fresh review and fix material findings with regression checks.
- [x] Record evidence and any unresolved scope accurately.

## Verification evidence

- Pinned NanoFaaS suite: **3,442 passed**; no behavior-test failures. The final
  CLI diagnostic change and its additional regression were then verified with
  the CLI/catalogue suite: **153 passed**.
- Checkout-free suite: **2,832 passed**, 14 source-contract modules skipped,
  375 marked tests deselected. The additional unsupported-runtime CLI regression
  also passes without a checkout.
- tui-toolkit: **141 passed**, 94% coverage.
- Both wheel and sdist builds pass. Both wheels install into an isolated offline
  environment; catalogue, CLI/TUI presets, validation assets, payloads and writable
  operator outputs pass the installed smoke test.
- Real k6 exports from four localhost requests verify 25% failures consistently
  in the detailed report, comparison and release interpretation.
- Ruff, format, both type checks, import contracts, Bandit and file hygiene pass
  for every changed/new file. `uv lock --check --offline` passes.
- Fresh review found source contracts missing the CI marker; fixed, including
  other contracts that skipped at runtime. No further material findings remained.

### Existing coverage limitation

Explicit `--cov-config=packages/nanolab/pyproject.toml` enables branch coverage
and the package's 90% threshold. The full suite reports **86.11%**, so that command
still exits unsuccessfully on coverage despite all tests passing. An isolated
copy of unchanged HEAD, against the same NanoFaaS pin and interpreter, reports
**86.09%**. Its sole test failure was an executable lookup with the venv absent
from PATH; rerunning that test with the venv on PATH passes and leaves coverage
unchanged. The threshold was retained. Workspace-root CI commands, as before,
report coverage without explicitly selecting that package coverage configuration;
the CI step is described as a combined report, not a threshold gate.

Live VM/cloud/Kubernetes execution was not performed. Runtime support for Rust
remains #57. Operation wrappers and cloud dependencies were retained for the
behavior/import reasons recorded in the design. No changes were made to the
operator's NanoFaaS checkout.
