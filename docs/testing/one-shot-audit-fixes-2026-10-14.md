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
