# One-shot CI repair — 2026-10-15

Baseline: NanoLab `2c325c5f98920f6ced820a2dc1c301f0cfcf2f99`,
[failed CI run 38709232497](https://github.com/Nanofaas/nanolab/actions/runs/38709232497).
Work was isolated on `fix/one-shot-ci`; the original NanoLab checkout and dirty
NanoFaaS checkout were preserved.

## Causes and changes

- The fake VM workflows read the actual runner's CPU and available memory.
  This caused 94 pure-test failures on CI. An explicit test fixture supplies
  simulated capacity to six affected test modules. Production capacity checks
  are unchanged, and tests for insufficient CPU, memory, external VMs and the
  owned logging backend still override capacity and verify rejection.
- The CI NanoFaaS pin `6570aaf0` predates the modules and five Rust workloads
  required by the bundled recipes. Published `main` at `d00d9f66` also lacks
  those workloads: testing a clean copy still produced 187 failures and 19
  errors. The compatible existing commit is
  `79a0b21a804cd3ec9466b4ed9fe6a6cba64855ba`, 36 commits above `d00d9f66`.
  With explicit user approval it was published as
  [`ci/nanolab-one-shot-79a0b21a`](https://github.com/Nanofaas/nanofaas/tree/ci/nanolab-one-shot-79a0b21a),
  without merging NanoFaaS or creating source changes there. The action pins
  that immutable SHA. Historical release references and recipe bytes remain intact.
- Ten type errors were resolved with explicit diagnostic-value/callback types
  and correct test-double/invalid-input handling. Production `assert` statements
  flagged by Bandit became runtime guards or existing first-cause assignment.
  All independent persistence and cleanup attempts remain in place.
- Script lint corrections document intentional diagnostic exception boundaries,
  make `check=False` explicit, parenthesize adjacent string literals, normalize
  imports and make two shebang scripts executable. AST comparison verified that
  the tuning/memory scripts are unchanged apart from the explicit subprocess
  default. No experimental values changed.

## Verification

Reproduction before fixes:

- A simulated host with 2 CPUs / 4096 MiB caused the owned-resource cleanup test
  to fail at the production capacity guard, before provisioning.
- `test_preflight_freezes_release_recipe_groups` failed against the old source
  with `Release recipe modules differ from guarded all selection`.
- Ruff reproduced 20 script findings; Bandit reproduced eight production asserts.
  The CI logs record all ten type errors.

Checks after fixes:

| Check | Actual result |
| --- | --- |
| Affected tests, with the same small host injected before fixtures | 184 passed |
| Full pure NanoLab partition | 4572 passed, 28 skipped, 392 deselected |
| NanoFaaS contracts against a clean archive of `79a0b21a` | 637 passed, also repeated with `--cov-append` |
| Combined coverage from the CI commands | 27,294 / 30,362 statements, 89.90% (console rounds to 90%) |
| All bundled recipes, Gradle `validateRecipe` | 32 passed |
| Representative plans and acquisition/release ordering from CI | Passed |
| TUI package tests | 51 passed; 93.71% coverage |
| Full pre-commit, including both type/import/security checks | All 15 hooks passed |
| Ruff packages and scripts, formatting, lock consistency | Passed |
| Both distributions built; installed wheel imports/assets/CLI and smoke script | Passed |
| Independent review regression selection | 201 passed; no important findings |

Commands follow the existing CI workflow: pure tests with an empty
`NANOFAAS_ROOT`, followed by `-m nanofaas --cov-append` against the clean pinned
checkout. No coverage exclusions or thresholds were changed.

## Review boundaries

GitNexus impact checks resolved low impact for the edited supervision/reporting
methods and backend factory. The observation builder returned `UNKNOWN`; direct
caller inspection found its launcher and simulated workflow tests. The final staged change analysis reports HIGH impact (27 symbols, 10 flows).
An earlier all-changes analysis after reindexing reported CRITICAL impact
(23 symbols, 298 flows); neither result substitutes for the independent review
of first-cause handling, archive invalidation and complete resource release. Full-text index construction
failed with a buffer-pool allocation error, and CLI flow output is abbreviated;
the graph is supporting evidence rather than an exhaustive proof. The broad final
flow expansion includes the newly annotated local `row` dictionary as a Property;
source inspection confirms its change only declares diagnostic value types.

No campaigns, experimental VMs/containers or releases were launched. The normal
remote CI smoke job remains part of verification. Frozen schedules, resources,
timeouts, acceptance thresholds and historical experiment evidence were not edited.

To repeat the two CI partitions from the repository root (with the workspace
installed and `NANOFAAS_ROOT` pointing at a clean checkout of the pinned SHA):

```bash
NANOFAAS_ROOT= uv run --locked --all-packages --all-groups pytest \
  -c packages/nanolab/pyproject.toml packages/nanolab/tests \
  -m 'not nanofaas' --cov-fail-under=0
uv run --locked --all-packages --all-groups pytest \
  -c packages/nanolab/pyproject.toml packages/nanolab/tests \
  -m nanofaas --cov-append
uv run --locked --all-packages --all-groups pre-commit run --all-files
```
