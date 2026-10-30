# Implementation ledger

Worktree /private/tmp/nanolab-gc-native-refresh, branch feat/gc-native-refresh.
Base 6f32a3f. Source NanoFaaS unchanged.

Task 1 RED: two new container profile tests fail because distribution is absent.
Ruling: use existing exact native dictionary comparisons; adding the expected
distribution preserves all mismatch rejection without new verification code.

Task 1 GREEN: 80 tests passed, including native metadata evidence/publication.
Task 2/3: seven runtime recipe profiles and CPU1/2/4 pilots implemented; no new
variant objects or host installer. Original default/profile identities preserved.
Task 4: phase GC counters use shared counter_delta; HTML uses existing comparison
series/charts and phase shading, with idle-origin alignment. Offline compare-report
now dispatches E01 and validates receipt hashes and saved order before rendering.
Review found missing offline dispatch; corrected and added a CLI regression test.
Verification: 286 comparison/CLI/plan tests passed (26 infrastructure skips), then
11 offline report tests passed after the last CLI update. Ruff passes; five import
contracts kept. Baseline basedpyright has five existing errors in one-shot tests;
final comparison is recorded separately. No source NanoFaaS changes.
Pending Task 5: real pinned validateRecipe/build/readiness, installed wheel smoke,
full Linux checks and future cloud pilot. No provisioning/merge/push performed.
Ruling: graph is stale and NanoFaaS is unchanged; do not infer build feasibility
from a stale graph or Python tests.

Additional verification: all seven recipes accepted by real validateRecipe on
NanoFaaS c0d15e3/plugin 0.1.2; logs /private/tmp/experiment-01-runtime-*.validation.log.
Wheel and sdist built in /private/tmp/nanolab-gc-wheel; extracted-wheel smoke
outside checkout resolves all seven recipes and three CPU presets.
Final basedpyright: exactly five baseline errors in unchanged one-shot tests;
no errors in changed code. Main checkout baseline independently reports five.
Task 5 remaining: real native builds/startup and full Linux CI gates.

Follow-up: actual measured E01 archive test passes without source checkouts and
rejects tampered snapshot hashes. RED CSV test showed GC counters missing from
CSV; added columns for runtime group only, original headers preserved.
Ruling: add two test-side non-None assertions to existing one-shot fixtures so
required type gate can pass; these make their existing assumptions explicit and
do not alter production code or experimental parameters. Baseline type failures
were reproduced before these edits.


## Final verification

All seven pinned recipes passed validateRecipe and real Linux ARM64 builds,
startup, metadata verification and correct QR response via the same Rust image.
Native G1 completed with 8 GiB compiler heap/four threads; the earlier 6 GiB
attempt was cancelled under heavy compiler GC and its log retained. Runtime
heap stayed 64 MiB for every profile. No NanoFaaS code changes were required.

Clean Linux CI: 5414 pure, 645 real-source contract and 53 TUI tests passed;
combined coverage 87.67%; types, lint, format, imports, security, lock, package
and installed-wheel gates all exit 0. Earlier Mac /proc failures and Linux
missing node/curl prerequisites were environmental and superseded by this run.
The two fixture assertions resolve the independently reproduced baseline type
errors. Old CLI rejection of the now-supported JVM runtime was corrected and
a positive subset/resume test added.

Final independent review reproduced a wrong resource-chart origin when the
load generator origin is absent. Added a red/green regression and suppress
those curves when alignment is unavailable. Post-review Linux: 317 passed,
types/package/wheel all exit 0. Actual G1 metrics demonstrated operation names
missed by the old filtered JFR query: added one optional unfiltered operation
time query, keeping labels and the existing query. No inferred total pause.
Final source-backed targeted suite including metrics: 365 passed; full CI
precedes these last two changes. Ruff and import contracts verified separately.

Evidence is preserved in docs/testing/2026-10-30-gc-native-refresh. Local build
smokes used a source archive with a synthetic Git identity; missing tracked
files are explicitly recorded, with no changed tracked build source. Not a
campaign publication receipt. Temporary test containers and networks are gone.
Remaining: managed Kubernetes pilot, calibration and final campaign proposal.
No cloud allocation, registry push, merge or performance campaign performed.

Final pre-commit rerun: 365 targeted tests passed, basedpyright 0 errors/0 warnings, Ruff check and formatting passed (660 files), git diff --check clean. The temporary host venv link was required for dependency resolution and removed after checking; a run without it had missing-import errors, not source failures.
