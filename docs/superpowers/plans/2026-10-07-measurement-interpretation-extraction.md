# Measurement interpretation extraction (#61, sixth slice)

> Implement inline with superpowers:executing-plans; one fresh whole-slice reviewer, no per-task agents.

**Goal:** Share the numeric interpretation used by reports/release without moving measurement selection, counter classification or qualification policy.

**Architecture:** Extend existing `sonata_tasks.metrics` with finite-number parsing, per-publisher counter deltas and point statistics; extend `sonata_tasks.k6` with summary-value readers. NanoLab reexports these five functions and keeps `is_counter` locally. No model, framework or runtime dependency is added.

**Tech stack:** Python3.12, stdlib math/collections/itertools, uv, existing pytest/type/import/wheel checks.

**Spec:** ../specs/2026-10-06-sonata-extraction-assessment.md, individual measurement/credential decisions and prerequisites.

## Global constraints

- Bases: NanoLabccbe1a3, Sonataccdae29, public coherent pair0.6.10. Work only in isolated worktrees; NanoFaaS CI pin `/tmp/nanolab-61-pinned` e7914be remains untouched.
- Public APIs: `finite_number(value:object,*,nonnegative:bool=False)->float`; `counter_delta(points:Sequence[Mapping[str,Any]])->float|None`; `point_stats(points:Sequence[Mapping[str,Any]],*,counter:bool=False)->dict[str,float|int]`; `k6_values(metrics:Mapping[str,Any],name:str)->Mapping[str,Any]`; `k6_value(values:Mapping[str,Any],name:str,*keys:str)->float`.
- Preserve valid flat/nested k6 values, first-present alias and existing missing/invalid metric errors. k6 values require JSON numbers; checks/http_req_failed rates are0..1. Prometheus values may be numeric strings; bool/NaN/Inf are invalid.
- Counters group full publisher labels before reset detection. Every publisher needs two samples; negative/invalid evidence is unavailable, distinct from a genuine zero delta. Sort timestamps only when all samples in that publisher have them; otherwise retain input order.
- Point stats sum matching timestamps, allow decreasing/negative gauges and keep missing/invalid evidence separate from zero. No returned statistic may be NaN/Inf; aggregation overflow yields invalid_points and delta overflow omits delta. Malformed/unorderable observations must not crash the reducer or become zero.
- Preserve existing report/release layouts and units. NanoLab owns names, `function_dispatch`, naming/type classification, windows/skew, required metrics, thresholds and release records. Credentials remain a separately assessed later slice.
- Engine remains stdlib-only, zero runtime dependencies, with no new engine API. Coordinate prepared0.6.11 only after checking availability; local consumer trial keeps public pins/lock0.6.10 until separately authorized publication.
- Original coverage gates remain unchanged. Consumer checks use direct venv commands/UV_NO_SYNC=1 during the explicit wheel trial. Full NanoLab coverage was86.19%/90%; report functional results and gate separately.

## Review focus

- First-present malformed k6 alias must fail even when later aliases are valid; flat/nested exports and bounds/errors remain compatible.
- Independent publisher resets, missing singleton publishers and mixed sample order must not invent increments or conflate unavailable with zero.
- Malformed labels/timestamps, bool/numeric strings and nonfinite values must retain unavailable evidence; sum/delta overflow must never produce nonfinite statistics.
- NanoLab classification must keep explicit publisher type ahead of suffixes and the `function_dispatch` exception; report/release metadata and policies stay local.
- Base-only installed wheels must support all pure readers without optional HTTP/provider dependencies; ordinary non-NanoFaaS inputs must demonstrate the contract.

## Task 1: Shared readers in existing modules

**Files:** Sonata metrics.py, k6.py, tests/test_measurement_interpretation.py, package README and version/lock sites.

**Interfaces:** Produce the five APIs above, preserving accepted observations and explicit unavailable/error semantics.

- [x] Baseline existing metrics/tool-catalog cases and NanoLab interpretation/Prometheus/release cases. Expected: functional pass.
- [x] Write shared tests first: flat/nested and missing k6, alias priority/bounds/invalid numbers; per-label reset/order/singleton/zero; negative gauges, same-time sums, malformed/unorderable points and arithmetic overflow. Run new file --no-cov. Expected: missing shared functions fail.
- [x] Implement minimal stdlib readers in existing modules and run new/existing focused tests. Expected: GREEN, no changed existing task behavior.
- [x] Check PyPI0.6.11 unused; coordinate versions/lock and document ordinary usage plus evidence boundaries. Run full catalogue/engine with explicit coverage configs, hooks, build and all six wheel configurations. Expected: unchanged shared gates pass.
- [x] Verify base-only installed wheel using ordinary application/job labels and two real k6 fixture exports, malformed/overflow evidence and standard JSON allow_nan=False. Commit shared change. Expected: independent package contract passes.

## Task 2: Consumer facade and delivery

**Files:** NanoLab metrics/interpretation.py, tests/metrics/test_interpretation.py, assessment and this plan.

**Interfaces:** Reexport the five shared APIs under existing names; retain the current local is_counter implementation, all consumers and public dependency declarations during the local trial.

- [x] Add meaningful RED consumer regressions for overflow/malformed reducer inputs and characterization of counter classification/flat-nested report/release behavior. Expected: existing overflow/shape defects fail; established policies pass.
- [x] Explicitly install built0.6.11 wheels only into the isolated consumer venv; replace duplicate mechanics with reexports. Run focused metrics/loadtest/release groups. Expected: GREEN without changing product policies.
- [x] Run full NanoLab with original coverage config and isolated CI pin, toolkit, all hooks, build and fresh installed CLI/assets smoke. Expected: functional pass, known coverage debt separately reported.
- [x] Commit consumer trial and obtain one fresh whole-slice review. Regrade findings; fix Critical/Important in one RED→GREEN pass plus affected full suites; record every ruling/deferred minor. Expected: reviewable shared diff and bounded trial.
- [x] Push shared Sonata branch and open PR; preserve consumer trial locally pending merge and separate publication authorization. Expected: no unpublished consumer pins or package publication.

## Decisions

- Ruling: Extend existing metrics/k6 modules and keep counter classification local — pure arithmetic is reused across reports/release, whereas names and function_dispatch are product policy — cost if wrong: five shared functions to maintain and a small local classifier remains.
- Ruling: Repair observed overflow/malformed-reducer failures at the shared numeric boundary — probes produced Inf/NaN or exceptions, which cannot be useful unavailable measurement evidence — cost if wrong: those previously failing cases now yield unavailable evidence rather than exceptions/nonfinite numbers.
- Ruling: Keep credentials as an autonomous later slice — private-file identity/permissions, typed remote transfer and sanitized cleanup form a separate lifetime contract; registry/cosign choices remain local — cost if wrong: the credential implementation stays local until separately assessed extraction.
- Pre-flight: Task1 APIs are reexported verbatim by Task2; consumer format/classification/regression policies remain local, and public0.6.10 pins stay intact until publication authorization.

- Final: Ruling: Credentials stay deferred — users get the explicitly assessed pure-number slice, while private-file and transfer lifetimes need their own typed cleanup contract — cost if wrong: reusable credential mechanics remain local longer.

- Final: Ruling: Public pins remain0.6.10 during the local wheel trial — users get a reviewable shared PR without an index dependency on unpublished0.6.11 — cost if wrong: consumer adoption must wait for merge and authorized publication.

- Final: Ruling: Do not normalize timestamp dates/units — callers supply consistently comparable keys and choose units; malformed numeric keys are now an Important validation fix — cost if wrong: callers must normalize heterogeneous time representations themselves.

- Final: Ruling: Leave existing scrape checks and unrelated report aggregation unchanged — those APIs have no changes and product aggregation remains local under the individual assessment — cost if wrong: separate preexisting behavior may need a later fix.


## Verification and delivery

- Shared commits ccdae29..a0078a7:619 catalogue tests/91.65%,227 engine tests/96.17%, unchanged90% gates; all hooks, build, six wheel configurations and ordinary base-only application/two real k6 exports passed.
- Consumer:254 focused,51 toolkit/93.71%, hooks, build and fresh installed CLI/assets passed. Final full original command:3469 passed in296.03s, exit1 solely at86.19% branch coverage versus90%, identical to the prior slice. No coverage gate changed.
- One fresh whole-slice review; three Important findings fixed in one RED→GREEN pass and covered through the consumer facade; no Critical/Minor findings and no deferred minors. All review rulings are recorded above.
- Consumer trial remains local with public pins/lock0.6.10. Publication requires separate authorization after the shared merge.

Shared PR: https://github.com/Nanofaas/sonata/pull/21 (head a0078a7). Consumer local trial commits ccbe1a3..0ddd6fb; operator main remains clean and unchanged.

GitHub verification: all nine Sonata jobs passed on both push and PR events
([PR CI](https://github.com/Nanofaas/sonata/actions/runs/37603287517),
[push CI](https://github.com/Nanofaas/sonata/actions/runs/37603279326)).
