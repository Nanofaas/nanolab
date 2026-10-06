# Owned process extraction implementation plan (#61, first slice)

> **For agentic workers:** Use `superpowers:executing-plans` to implement this
> plan inline. Checkboxes track independently verifiable deliverables.

**Goal:** Share NanoLab's Linux command ownership implementation through Sonata
without changing cancellation, quotas, descendant cleanup or diagnostic behavior.

**Architecture:** Export the existing runner from `sonata_tasks.process`; keep its
stdlib-only implementation in `sonata_tasks.owned_process` so diagnostic helpers
can copy the same installed distribution module without installing the engine.
NanoLab retains a compatibility import and product-specific receipt interpretation.

**Tech stack:** Python 3.12, Linux procfs/subreaper/pidfd, uv, pytest, Buildx.

**Spec:** `../specs/2026-10-06-sonata-extraction-assessment.md`.

## Global constraints

- Keep `managed_process_resource` portable and its existing behavior intact.
- No Sonata imports from NanoLab or NanoFaaS; no engine runtime dependencies.
- Preserve the runner's public constructor, `run`, `stop`, `launched` and result.
- Published pair precedes the three NanoLab pin changes and lock update.
- Baseline published pair was 0.6.5; prepare coherent 0.6.6 distributions.
- NanoFaaS checkout stays read-only. Work in `/tmp/sonata-issue61` and
  `/tmp/nanolab-issue61`; validate adoption against built wheels in an isolated
  environment before publication, without changing the workspace dependency pins.

## Review focus

- Detached multi-level descendants: cleanup cannot signal unrelated processes.
- Caller exceptions and cancellation before launch: preserve the primary error.
- Summary framing: markers may span reads; log and summary share a quota.
- Diagnostic helper import: copied module must work without Sonata installed.
- Dependency boundary: current main remains runnable on published 0.6.5 until
  the reviewed 0.6.6 pair is published and the adoption pins/lock can change.

## Task 1: Sonata command ownership

**Files (Sonata):** `packages/sonata-tasks/src/sonata_tasks/{process,owned_process}.py`,
`packages/sonata-tasks/tests/test_owned_process.py`, package READMEs/catalogue,
both pyprojects, `src/sonata_engine/__init__.py`, `uv.lock`.

**Interface:** The unchanged `OwnedCommandRunner`, `OwnedCommandResult`,
`run_owned_command` signatures from NanoLab `tasks/soak/processes.py`, exported
from `sonata_tasks.process`. Implementation module is stdlib-only.

- [x] Transfer the generic runner regression tests and shared-runner workload
  cases to Sonata. Add non-NanoFaaS argv, summary/combined-quota, pre-cancel,
  reuse, invalid-input and unsupported-host cases; verify RED against 0.6.5.
- [x] Move the implementation without redesigning the supervisor; annotate and
  format to Sonata rules. Preserve the existing managed-process resource.
- [x] Run the full catalogue/engine suites and required lint/type/import/security
  checks. Build/install both wheels and execute a command outside the checkout.
- [x] Prepare coherent 0.6.6 version sites and lock, without publishing remotely.

## Task 2: NanoLab adoption and diagnostic helper distribution

**Files (NanoLab):** `tasks/soak/processes.py`, `tasks/soak/helper_build.py`,
`assets/diagnostics/diagnostic-helper.Dockerfile`, helper documentation,
`tests/soak/{test_helper_build,test_workload,test_process_regressions}.py`.

**Interface:** NanoLab's existing process import path remains an adapter. Buildx
receives a named `sonata` context resolved from the installed runner module;
the Dockerfile copies `owned_process.py` as the worker's `processes.py` and
verifies its SHA-256. Record the installed Sonata version as build metadata.

- [x] Add failing helper tests: named context has the runnable standalone module;
  transmitted digest matches its bytes; version comes from the distribution.
- [x] Replace the local supervisor with reexports; remove transferred generic
  tests, preserving product integration tests. Update helper context/checksum
  arguments and Dockerfile; keep output/build metadata failures observable.
- [x] Verify the copied module executes under isolated Python with no installed
  Sonata; validate the actual Dockerfile's copy and digest-check commands.
- [x] Run NanoLab suites, required hooks, wheel tests and installed smoke in an
  isolated environment consuming Task 1's wheels. No Git dependency override.
- [x] Get a fresh review of both working diffs and fix material findings.
- [x] After publication of engine then tasks 0.6.6, update all three exact pins,
  regenerate NanoLab's lock and verify installation from the public index.

## Delivery boundary

Publication is a distinct external action: prepare tested, reviewable changes
before requesting it. Publication was authorized after the Sonata PR merge and
completed before NanoLab's pin/lock update. This slice does not close #61.

## Verification and delivery status (2026-10-06)

- Sonata commit: `a6f02c9bac5a479db423fbc49744195118603c58` on
  `feat/owned-command-runner`; PR: <https://github.com/Nanofaas/sonata/pull/16>.
  All GitHub CI checks passed. Catalogue: 397 passed, coverage 90.40%; engine:
  227 passed, coverage 96.17%. Required hooks passed. Both 0.6.6 wheels and
  source distributions built; an ordinary command passed in an empty environment
  containing only the two installed wheels, outside both checkouts.
- NanoLab adoption was prepared in `/tmp/nanolab-issue61`, on
  `feat/61-owned-processes`. Before publication the three dependency pins and
  lock remained 0.6.5, with locally built 0.6.6 wheels only in this isolated
  validation environment. After publication they were updated to 0.6.6 and
  `uv sync --locked` replaced the local trial wheels with index distributions.
  The original checkout and its environment were not changed.
- NanoLab: 3,443 tests passed when run alone. The command still exits with a
  coverage failure: 86.21% against the required 90%. The unchanged baseline
  measured 86.16% after separately rerunning its 34 release CLI cases. Initial
  concurrent baseline/adoption runs collided on the shared release identity
  lock; separate runs resolved all functional failures. The coverage requirement
  was retained. Toolkit: 51 passed, coverage 93.71%. Required hooks, wheel
  builds and installed-wheel smoke passed.
- A real ARM64 container, using the locked Python base and the helper Dockerfile's
  actual copy/checksum instructions, imported the standalone module with Python
  `-I -S`, executed a command and reaped a detached descendant. A build supplied
  with an incorrect SHA-256 failed with the expected digest-mismatch error. The
  uniquely labelled test image was removed. This validates module distribution;
  it does not claim a complete JDK/MAT helper build.
- Fresh independent review found no critical, important or minor findings;
  the reviewer also ran the 42 Sonata process tests and 60 NanoLab helper/workload
  tests successfully.
- Sonata PR #16 was merged as `b85eb8c449799dd5e3bc43c6023e731837ae1666`.
  After explicit publication authorization, annotated tag `v0.6.6` was pushed
  at that commit. [Release workflow](https://github.com/Nanofaas/sonata/actions/runs/37498291312)
  completed successfully, publishing engine before tasks. PyPI contains both
  wheels and source distributions; tasks pins engine 0.6.6, and engine has no
  runtime dependencies.
- NanoLab's three exact pins, lock and documented pin examples now use 0.6.6.
  No Git dependency was introduced. Required hooks passed again; toolkit passed
  all 51 tests with 93.71% coverage. Both NanoLab distributions built and were
  installed in `/tmp/nanolab-issue61-public-installed`, with dependencies
  resolved from PyPI. Installed CLI and asset smoke passed outside the checkout.
  The published runner's bytes match the reviewed implementation, and the helper
  checksum/version, standalone import and an ordinary command were verified.
- Final full NanoLab verification uses a fresh temporary clone at the exact CI
  source commit `e7914be065e844776af57fe9e449bce7f12e03c5`. The previous exported
  fixture omitted 23 tracked paths matching ignore rules when it was re-committed;
  final validation avoids that fixture. The operator checkout remains read-only.
  With published dependencies, all 3,443 tests passed in 300.53 seconds. The
  command exits 1 solely because coverage is 86.18%, below the retained 90%
  requirement. Log: `/tmp/nanolab-issue61-public-tests.log`.
- First-slice implementation and public-dependency verification are complete.
  Commit, push and PR delivery were explicitly authorized after validation.
  The adoption branch is delivered for review, with its coverage gate still
  red and the worktree retained. Other extraction candidates retain their
  individual assessments; this PR must not close the entire issue.
