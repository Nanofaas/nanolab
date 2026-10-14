# One-shot audit fixes — offline validation, 14 October 2026

Plan and original audit live in the sibling NanoFaaS checkout:
`docs/superpowers/plans/2026-10-14-one-shot-audit-fixes.md` and
`docs/experiments/one-shot-audit-2026-10-14/README.md`.
Implementation starts at NanoLab `8cb9756` on isolated branch
`fix/one-shot-audit-2026-10-14`. No campaign, VM/container acquisition, merge,
push, experimental runtime change or historical freeze rewrite was performed.

## Regression evidence

| Correction | Observed red check | Green check |
| --- | --- | --- |
| Service first cause | 6 failures; 1 successful control | supervisor: 37 passed |
| Independent inventory/report | 3 failures; 8 controls | launcher + supervisor: 81 passed, 6 skipped |
| Archive outcome/ownership | 11 failures; 7 controls; separate unwritable-receipt failure | supervisor: 56 passed |
| Scenario-relative telemetry | 2 relative-path failures; 3 controls | public CLI + VM resources: 45 passed |
| Unsupported image overrides | 9 validation failures; 15 defaults/builder controls | scenario and generic image suites: 111 passed, 35 skipped |
| Active launcher identity | 1 drift failure; 4 package/identity controls | all 5 identity checks passed |
| Writable archive with unwritable raw receipts | archived receipt incorrectly completed | regression passed after direct owned-archive invalidation |
| Review: receipt failure without archive | 2 failures using actual result/status directories and real renderer | both passed after report finalization ordering fix |

The corruption regression executes the real SHA verifier. Sonata workflows
execute with simulated resources; external inventory calls are injected at the
subprocess boundary. The renderer, filesystem, dependency scheduling and
compensation remain real. Reference and SciPy subprocess tests reuse the preserved
Python environment and source; the HTTP probe opens only a loopback test server.

Final combined regression command covers `test_supervision.py`,
`test_first_observation.py`, `test_public_cli.py`, `test_scenario_one_shot.py` and
`test_observation_workflow.py`: **173 passed, 6 skipped**.
Full one-shot plus heterogeneous/scenario configuration: **1,015 passed,
6 skipped, 20 subtests passed**. The entire NanoLab package also passed: **4,311 passed, 415 skipped,
20 subtests passed**. Package skips comprise 406 NanoFaaS checkout contracts
without `NANOFAAS_ROOT`, the same six observation integrations, and three
JVM/native-inapplicable cases. One-shot skips are two actual runtime/JDK contracts and
four preserved observation/tuning/runtime artifact integrations whose required
environment variables were not supplied. Skipped integrations were not tested.

Commands use the package pytest configuration and
`PYTHONPATH=packages/nanolab:packages/nanolab/src:packages/tui-toolkit/src` with
`uv run --frozen --all-packages --all-groups --no-sync python -m pytest
-c packages/nanolab/pyproject.toml ... -q --no-cov -ra`.
`--no-sync` reuses the existing environment; `PYTHONPATH` selects the worktree
sources. These checks do not claim coverage percentages.

Reference test variables point to existing preserved artifacts:

```text
ONE_SHOT_REFERENCE_ROOT=/tmp/oneshot-first-experiment-20261012/observation-preparation-02/reference
ONE_SHOT_REFERENCE_PYTHON=/tmp/oneshot-first-experiment-20261012/observation-preparation-02/python-env/bin/python
ONE_SHOT_CURVE_PYTHON=/tmp/oneshot-first-experiment-20261012/observation-preparation-02/python-env/bin/python
```

Ruff passes for all changed Python files with package settings. Package files use
automatic configuration discovery so test per-file ignores resolve correctly;
the standalone observation script uses explicit package configuration. Diff
whitespace checks pass. The three published campaign scenarios validate read-only
and contain no rejected image overrides. Future freezes must capture the fixed
package and active launcher; existing fingerprints are never bypassed.

## Review and graph limitations

One independent whole-series review found an Important writable-report false
success after a first terminal receipt persistence error. Two real filesystem
regressions failed before the final fix and passed afterward. No other Critical,
Important or Minor findings, deferred items or declined-to-judge behaviors were
reported. The fix received fresh automated verification, not a second review.

GitNexus 1.6.12 was found in the local npm cache. Indexes were built in `/tmp`
without injecting repository instructions. FTS/BM25 search could not be built;
impact/diff analysis used the graph and source confirmation. Freshness was checked and graph change
analysis run for each scoped change and the final branch range. The first change
analysis failed with sandbox `spawnSync git EPERM`; it was rerun with escalation
against the committed first range. Subsequent precommit analyses ran escalated.

`service_resource` and `run_observation` report LOW; `_run` initially reported
CRITICAL/HIGH through 154 dependants and 148 process associations. `_scenario`
was not resolved and Pydantic `validate_workflow` had UNKNOWN risk. Source searches
confirmed production callers and Pydantic consumers; UNKNOWN was not treated as
safe or unused. The index warns of process truncation (1,032 entry candidates,
889 callees and 30 walk budgets omitted) and callable-resolution caps. Final complete-range analysis reported 10 changed files, 73 symbols, 5
execution flows and medium risk. The affected flows include scenario loading
for plan/run/compare and service acquisition/status. Some individual diff
hunks do not overlap indexed symbols. These results are partial graph evidence,
not an all-clear; real regressions and source review provide the behavioral checks.

## Follow-up to the independent finalization review

The review of `14f261e` identified two additional P2 finalization defects.
The preceding sections describe the first audit series at its original revisions;
they do not claim `14f261e` already handled these follow-up cases correctly.
The approved follow-up plan is
`nanofaas/docs/superpowers/plans/2026-10-14-one-shot-finalization-review-fixes.md`.
Implementation continued natively in the same worktree and branch:

- `56fc7c4`: persist deferred interruptions with or without an archive.
- `981eb4e`: independently update writable reports and invalidate archive outcomes
  after partial report persistence failures.
- `01fab85`: persist changes observed during terminal receipt writes and retain
  secondary report-channel notes under the existing primary exception.

Baseline supervisor/report checks passed 73 tests. Task 1's new regressions
observably failed in two no-archive signal cases while ten controls passed;
the complete supervisor then passed 70 tests. Task 2 had 17 red failures and ten
controls; supervisor/report then passed 112 tests. The final independent review
found two Important cases within the same scope: signal adoption before the
persistence guard and secondary output errors lost while preserving an earlier
workflow cause. Ten further real-I/O/signal regressions failed before the final
fix and passed afterward; supervisor/report passed 122 tests. No Critical, Minor,
deferred or declined-to-judge findings were reported. The single review fix pass
was verified through regressions and the broader suites below, without a second
independent review.

### Final follow-up verification

- Combined supervisor, report, launcher, public CLI, observation workflow and
  scenario configuration: **237 passed, 6 skipped**.
- Full one-shot plus heterogeneous/scenario configuration: **1,063 passed,
  6 skipped, 1 deselected, 20 subtests passed**, with the loopback HTTP test
  subsequently run alone outside the denying sandbox: **1 passed**. All selected
  tests were exercised; the isolated HTTP test was not changed or counted as a
  skipped integration.
- The entire NanoLab package completed on the final code: **4,360 passed,
  415 skipped, 20 subtests passed**. The extra 409 skips comprise 406 NanoFaaS
  checkout contracts without `NANOFAAS_ROOT` and three JVM/native-inapplicable
  cases. An earlier extra package-wide sandbox run stopped progressing at 61%
  and was interrupted/terminated only in its owned pytest process; the full
  final run above completed with the existing local-test execution permissions.
- The six optional skips require two actual runtime/JDK contracts and four
  preserved observation/tuning/runtime artifact settings. They were not exercised.
- Reference/SciPy tests reused the preserved source and Python environment listed
  above. Commands additionally set `PYTHONDONTWRITEBYTECODE=1`,
  `UV_PROJECT_ENVIRONMENT=/home/michele/Documenti/nanolab/.venv` and
  `-p no:cacheprovider`, explicitly selecting this worktree's sources. No new
  environment, dependency or campaign freeze was created.
- Ruff, format checks and whitespace checks passed for the four changed Python
  files. The existing real package fingerprint was checked for sensitivity to
  changes in both `one_shot/supervision.py` and `one_shot/report.py` using temporary
  copies; installed/historical bytes and drift checks were unchanged.

The original independent reproducer was rerun without editing it. New output is
saved beside the historical evidence as
`nanofaas/docs/experiments/one-shot-audit-2026-10-14/branch-review/fixed-01fab85-reproduction.jsonl`.
Assertions on actual saved result/status/JSON/HTML confirm healthy exit 0,
SIGINT 130, SIGTERM 143, and report-I/O failure exit 1 in every writable output.
For report-I/O failure the unavailable raw HTML remains diagnostic evidence;
raw JSON and all writable archive outcomes are failed with
`archiveVerified=false`. The original branch/main reproduction files and script
retain their SHA-256 hashes.

HTML/JSON summaries may be reconciled only when their scientific payload matches
and their prior `operationalOutcome` differs. Missing, ambiguous or malformed HTML
summaries and divergent science remain errors; an unavailable/invalid output is
not reconstructed from the other channel. Each valid existing output is attempted
independently, preserving chart bytes outside the selected summary. First causes
and signal codes survive, with all encountered channel limitations retained as
notes. No workflow/acquisition/measurement or failed verification is retried;
existing unowned archive destinations remain protected.

GitNexus freshness was checked and each scoped diff analyzed before commit.
Initial `_run`, `_write_result` and finalizer impacts were LOW; the archive-control
invalidation impact later reported CRITICAL with 87 process associations and was
reported explicitly. An incremental index update left `incremental-in-progress`
and unresolved symbols; these UNKNOWN/empty results were checked against source
callers and not treated as safety evidence. A forced full rebuild completed and
its contents matched all 1,483 covered files. The final graph again resolves
CampaignSupervision and the changed production symbols. Process truncation and
FTS/BM25 failure remain limitations. Final comparison against `14f261e`
reported six changed files, 38 symbols, zero resolved affected flows and LOW
aggregate risk; these counts do not negate the earlier CRITICAL impact warning. The source review and real regression tests,
not zero graph counts, establish the checked behavior.

No protocol, load, rounds, CPU/RAM, pools, timing limits or experimental criteria
changed. No VM/container acquisition, campaign, merge, push, runtime integration
or unrelated filesystem cleanup was performed. Existing fixes and modifications
were preserved; the worktree/branch remain available. Documentation is committed
separately; the NanoFaaS plan and new reproduction evidence remain uncommitted.
