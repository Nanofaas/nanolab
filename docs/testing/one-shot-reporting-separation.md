# One-shot reporting separation — verification

Implementation worktree: `/home/michele/Documenti/nanolab/.worktrees/one-shot-reporting`.
Branch: `feat/one-shot-reporting`, based on reviewed `main` at
`12d3215cb8493b020279a64b7d99e58c43f97186`; all earlier audit/finalization fixes
are ancestors. The original checkout and other worktrees were preserved.
No campaign, VM/container provisioning, release, merge or push was performed.

## Implementation and contracts

- `CampaignRecorder` owns append-only event/progress files, atomic status and
  independent result writes. Snapshot and first-cause decisions remain supervised.
- `presentation.py` is the UI-independent Sonata contract/borrowed-sink adapter.
  Concrete adapters live in `views.py`: this extra module keeps all five existing
  import-linter contracts intact, including transitive runtime/CLI/TUI boundaries.
- Console uses `ConsoleProgressSink`; TUI uses the existing controller, dashboard,
  sink and aggregator. JSON is explicitly selected for compatibility. `none` has
  the same health, signal, recording and cleanup machinery without terminal I/O.
- Display and storage failures from worker callbacks wake blocking workflow waits
  through the existing first-fault signal mechanism, independently of cadence.
  Views run outside the health lock; view calls are serialized without a queue.
- TUI refresh uses existing events, heartbeat and keypresses synchronously instead
  of Rich's unobservable refresh thread. The listener is joined before final
  archive verification. Broken rendering is disabled while terminal restoration
  still executes. Campaigns never wait for acknowledgment; existing interactive
  workflows still do, on both success and action failure.
- Sonata 0.6.14 does not put resource-operation kind in `WorkflowEvent`. Release
  fault deferral uses its `Resource.release_title` vocabulary (`Release ...`).
  Only root lifecycle events change the release guard; log rows emitted inside
  a release cannot clear it. Started/passed and release-body logging faults have
  explicit cleanup regressions. An upstream
  change to that vocabulary must adapt the guard; no engine upgrade was made.
- Live views close before the authoritative control copy/hash verification.
  Console/JSON have no owned background lifecycle and keep the final summary.
  Caller-provided sinks are borrowed and retain their caller-owned lifecycle.
- The parser runs before frozen imports and accepts flags at every positional
  boundary. New normal freezes inventory Rich/TUI toolkit/Typer sources and
  versions, all NanoLab modules and the launcher. Old freezes remain unchanged.

## Verification method

All tests use real Sonata execution with simulated operational I/O and real
renderers, including an in-memory Rich console. Only filesystem/output/listener
boundaries are fault-injected. Request 429/503 rows remain raw observations.
The actual observation DAG runs across all four views with both 9 x 360-second
and 54 x 60-second frozen schedules simulated; no experimental settings changed.

Use the existing environment and worktree sources:

```bash
export PYTHONDONTWRITEBYTECODE=1
export UV_CACHE_DIR=/tmp/nanolab-uv-cache
export UV_PROJECT_ENVIRONMENT=/home/michele/Documenti/nanolab/.venv
export PYTHONPATH=packages/nanolab:packages/nanolab/src:packages/tui-toolkit/src
export ONE_SHOT_REFERENCE_ROOT=/tmp/oneshot-first-experiment-20261012/observation-preparation-02/reference
export ONE_SHOT_REFERENCE_PYTHON=/tmp/oneshot-first-experiment-20261012/observation-preparation-02/python-env/bin/python
export ONE_SHOT_CURVE_PYTHON="$ONE_SHOT_REFERENCE_PYTHON"
uv run --frozen --all-packages --all-groups --no-sync python -m pytest \
  -c packages/nanolab/pyproject.toml packages/nanolab/tests \
  -q --no-cov -p no:cacheprovider -ra
```

Imported NanoLab/TUI paths were checked against this worktree; Sonata comes from
the existing pinned environment. `--no-cov` is test evidence, not a coverage claim.
The `/tmp` uv cache avoids the sandbox's read-only default cache.

RED→GREEN evidence includes recorder extraction (5 initial failures), sink
composition/headless/background view faults (11), adapter/lifecycle selection
(23), refusing Rich's second render after a fault (1), parser/stage routing (27),
background recorder wakeup (2), disposal signals without archives (2) and status
body-error precedence (1). The final independent review added release-body
logging failure (1) and closing status diagnostic failures in display/storage
channels (2), all observed RED before the fix and GREEN afterwards. Existing app tests caught and verified restoration of
interactive acknowledgment after action failure. Focused finalization/UI checks:
212 passed before whole-package verification. Detailed final results follow below.

## Review limits

GitNexus was indexed in `/tmp`, with context/impact checks and source caller
inspection. `event()` was CRITICAL in pre-edit impact; the Task 5 diff was HIGH
(37 indexed symbols, 14 flows). The index reports capped/missing execution flows,
candidate truncation and unresolved cross-language fields; absent graph edges
are not evidence of absence. The final comparison against `12d3215` reports CRITICAL impact: 15 tracked files,
170 symbols and 26 affected flows before the documentation commit. The CLI
truncates displayed symbols/flows even with a larger limit. Index construction
reports 1,049 omitted entry-point candidates, 957 omitted callees and 31 bounded
walks; these limits prevent claiming exhaustive graph coverage. The sandboxed
rebuild failed and produced no usable overlap; a successful local rebuild with
the required permission preceded the reported comparison. Independent review
and direct source inspection supplement this partial graph. Optional external integrations are never counted as passing.

## Independent review and decisions

One fresh-context review of the whole branch found two Important issues, no
Critical or Minor findings, and no behavior it declined to judge. Both issues
were reproduced and fixed in one pass:

- A root `log.line` emitted inside resource release used to clear the release
  guard, so rendering failure could interrupt cleanup. Only root lifecycle
  events now update it; the regression verifies that the owned release finishes.
- `status.finished` could mask an original task exception when its display or
  storage failed. Closing diagnostics now retain that original exception as the
  first cause, attach the secondary failure and finish owned compensation.

No additional independent re-review was claimed. The fix pass is verified by
three RED→GREEN regressions and the final suite below. No minors were deferred.

Execution decisions, in order:

1. Use `/tmp` for the uv cache because the default cache is read-only in the
   sandbox. Dependencies are unchanged; the tradeoff is a separate local cache.
2. Introduce the JSON adapter during composition so existing stdout regressions
   explicitly opt in. The cost is earlier test migration, not a new schema.
3. Use Sonata's root `Release ...` lifecycle vocabulary to defer display faults
   during owned release; no resource kind is exposed. If upstream changes this
   vocabulary the guard must change; lifecycle and release-body regressions cover
   the currently pinned engine.
4. Put concrete adapters in `views.py` to preserve transitive import contracts.
   The cost is one additional module in future normal source fingerprints.
5. Disable Rich autonomous refresh and reuse synchronous event/heartbeat/key
   refreshes so faults are supervised. When no events arrive, the next refresh
   waits for the existing heartbeat; no monitoring cadence was changed.
6. Let factories return a plain sink or a live context. Console/JSON retain the
   final summary; owned TUI lifecycle ends before authoritative verification.
   Any future sink with owned activity must use the context form. Borrowed sinks
   remain caller-owned.

## Final results

- Final presentation/launcher regressions: **135 passed, 6 skipped**. This run
  includes the three review regressions and the formatting-only launcher edit.
- Ruff and formatting checks passed for all 13 changed package Python files and
  the launcher with the package configuration; `git diff --check` passed.
- Import-linter: **5 kept, 0 broken**, 395 files and 3,164 dependencies analyzed.
- Basedpyright, using the existing venv: **10 errors**, exactly the same diagnostics
  as the untouched `main` baseline. They concern the pre-existing optional
  `add_note` path in supervision, logging metadata types, and existing tests in
  evidence fairness, Loki ingestion, rewindow and supervision. This is no new
  type regression, but it is not a clean whole-package type check.
- Full `packages/nanolab/tests`: **4,516 passed, 415 skipped, 20 subtests passed**
  in 228.83 seconds. This includes all one-shot tests, heterogeneous/scenario
  configuration, console/release simulations and shared TUI/app regressions.
  Optional skips: 406 require `NANOFAAS_ROOT`, four require preserved observation/
  tuning/runtime artifacts, two require native sources/JDK, two are native-only
  ELF cases and one is a JVM-only installed-library case. None counts as passed.
- The initial sandbox full run was incomplete: six tunnel tests and one owned
  clock-probe test were denied loopback, and a synthetic asyncio test stalled.
  The isolated asyncio test passed with the required permission; nine tunnel/
  probe checks passed likewise. Only this session's stalled pytest processes
  were stopped. The full result above comes from the subsequent permitted run,
  using the same sources, frozen dependencies and existing reference environment.

## Revisions

| Commit | Result |
| --- | --- |
| `3d32a81` | Recorder extraction |
| `a2921a1` | Supervision/presentation composition |
| `a2a50bf` | Existing console/TUI adapters and lifecycle |
| `ca1be50` | Launch selection and frozen dependency inventory |
| `623db8c` | Equivalent supervision matrix and finalization regressions |
| `160ff18` | Review fixes for release logging and closing diagnostics; type narrowing |

The documentation commit follows these code/test commits. The external approved
plan is updated in place, outside this repository; its pre-existing untracked
status is preserved. Branch and worktree are kept for inspection, without merge
or push.
