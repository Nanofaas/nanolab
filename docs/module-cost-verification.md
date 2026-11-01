# Module-cost implementation verification

The implementation is on `feat/module-cost`. It incorporates `origin/main`
`7dd4613` (Sonata 0.6.20 and azure-vm-sdk 0.9.0). Local main was fast-forwarded
to that upstream revision; the feature has not been merged into main or pushed.

The independent review identified three measurement issues. Regression tests
first reproduced each and then verified the fixes:

- Loaded resource windows now follow actual request timestamps. Generator clock
  anchors exclude staging/download time and reject uncertain or drifting clocks.
- Effective concurrency needs complete time coverage and fresh scrape timestamps;
  one sample or a stale Prometheus lookback value cannot qualify the whole arm.
- Cross-block compatibility includes implementation/dependency content, VM
  configuration, machine/kernel and capacity, alongside the scientific protocol.

End-to-end CLI checks also caught missing endpoint/tunnel routing. The workflow
now uses the normal remote URL and Prometheus tunnel path. Partial executions,
kept infrastructure, stale results and foreign endpoints are rejected early.
The TUI and catalogue recognize the new Azure-only workflow.
Import-contract checks caught two upward dependencies: the shared log task now
lives under `tasks`, and the ownership validator under `config`; all callers were
updated without duplicating either helper. All five import contracts pass.

Verification completed locally: 92 workflow/shared-helper regressions, 81 CLI
and role-binding regressions, 61 B1/loadtest regressions, two real k6 loopback
cases and four paper archive/render checks. These groups overlap. The package's
Ruff and basedpyright checks pass. B1/loadtest test doubles were annotated to
resolve nine pre-existing type errors without altering production behavior.
Final local checks additionally pass: 104 regressions across the affected
loadtest/B1/B6/module-cost paths, two real k6 loopback cases, four paper checks,
package formatting and Bandit. Both workspace distributions build; the NanoLab
wheel contains the workflow, shared diagnostics, preset and both k6 assets.
Linux verification on the existing controller completed with 6,217 passed,
59 skipped and 55 failures in the first full run (6,331 collected). All 55
failures were then resolved and rerun: 50 passed in the first recheck and the
remaining five passed in the final recheck. A further 116 affected-path tests
passed on the final source tree. This is a full run plus focused rechecks,
not a second clean full-suite invocation.

The initial failures were 48 Node.js PATH failures, four `/tmp` space failures,
one missing `uv` PATH entry, the import-contract violations described above,
and a historical P24 fixture comparison. The latter now explicitly checks that
five optional fields remain disabled; its historical workload settings are
unchanged. Only our two terminated pytest processes' temporary directories were
moved from `/tmp` to `/data`, preserving their contents and recovering 2.8 GiB.

The isolated Linux checkout is `/data/work/nanolab-module-cost-e89c54b` on the
existing controller; logs are under `/data/work/module-cost-verification-files`.
The final test environment uses the locked dependencies, `NANOFAAS_ROOT` at the
isolated NanoFaaS checkout, `TMPDIR` under `/data`, and PATH entries for
`/home/michele/.local/bin` and the locked `nodejs_wheel/bin` directory. NanoFaaS
source revision: `b1cb1608f414df87fb039783aa8ecd6715b7db8e`.

A macOS full-suite attempt was not a supported Linux qualification: it also
encountered Linux process/procfs requirements and sandbox restrictions. The
Linux run and explicit loopback checks above are the verification evidence.

## Decisions made during implementation

- Use an isolated linked NanoLab worktree under `/private/tmp`, because the
  managed workspace tool targets only the paper repository. Commits remain in
  NanoLab's shared Git store even if temporary working files are removed.
- Implement inline with one independent final review; both JVM and native
  remain experimental arms. No restriction to native-only execution was inferred.
- Reuse existing recipes, provisioning, platform resources, source inventory
  verification and collectors. A defect in a shared helper can affect this
  workflow too; focused regressions cover the reused paths.
- Preserve explicitly requested native CPU limits instead of silently replacing
  them with one CPU. Existing callers that relied on their explicit value being
  ignored now receive the requested limit; a regression test covers this change.
- Freeze source identities at acquisition and settings at preparation. Operators
  must retain the collected protocol, not infer the source revision from a recipe.
- Report readiness as container-running to Pod Ready, at Kubernetes timestamp
  resolution (one second). This does not support subsecond process-start claims.
- Use available VM CPU/physical-memory ceilings for the build options supported
  by the shared recipe path. Record commands and capacity; upper bounds do not
  reserve resources or guarantee freedom from OOM.
- Leave physical Azure success and native retention to a qualification run.
  Failure remains possible and is archived; no cloud campaign was launched.

There are no deferred minor review findings. Frozen campaign input files are
also in `nanofaas-paper/experiments/module-cost/manifests/mc-c481a04bfd0f/`.
