# Public CLI validation on Multipass

The three-node local topology has two edge VMs and one terminal cloud VM on a
shared ARM64 operator host. These are workflow validation results, not Azure or
scientific performance claims. The preserved raw evidence distinguishes SDK
handler executions, exact known proxy refusals, censored outcomes and generator
deficit. Missing telemetry is unavailable, not zero.

## Provenance and reproduction

The operator commands and YAML configurations are preserved alongside this file.
Historical NanoFaaS runtime source: `3958d488a46bf608f749d9c1f673c984447c08a7`,
whose terminal execution-ID correction is based on
`b1aa7f65c7ed0a70c2b94f37092fe62b7e877934`. The latest prerequisite correction,
`d00d9f666b8a200480edeee6c203656c8b4edf3a`, preserves remote failure diagnostics.
The distribution receipt pins the build recipe, source and image bytes. NanoLab
manifests additionally pin the actual package bytes used for each campaign;
runtime receipts record source dirtiness explicitly.

Artifact references retain the original operator paths. To reproduce elsewhere,
copy/extract the referenced files and update both path and SHA-256 explicitly;
do not reuse a local profile on a different image, input, resource configuration
or environment. Docker archives are deliberately excluded from this dossier;
build the recorded recipe/source, then recalibrate and qualify if bytes differ.

Files exceeding the repository's single-file limit are stored as reproducible
`.gz` files. Each `artifact-receipts.json` records the original path, byte count
and SHA-256; decompression restores the original bytes. Plotly HTML reports are
operator artifacts; compact numerical report JSON and raw observations remain
in the dossier.

## Historical calibration and forecast-only qualification

`calibration/` passed with 144 direct SDK measurements and six capacity cells,
covering one and two physical replicas on every node. The fitted service demand
is approximately 0.11149 seconds. The exact profile and per-node measurements
are preserved.

`qualification-rejected/` is the first independent timing run: one of 20 samples
was censored during the first one-to-two-replica transition. Preparation took
about 12.34 seconds, outside the declared budget; clock measurements were
healthy. The exact transient cause remains unresolved. This run was rejected,
not removed or folded into successful quantiles.

`qualification/` is a separate fresh run with the same image/profile/protocol:
20 samples, zero censorship, all matrix cells independently qualified. Period
20 seconds, lead 1.9 seconds; worst conservative cell q90 wall duration about
0.11668 seconds. This small local qualification does not establish a universal
tail bound. Subsequent comparison epochs independently enforce readiness.

## Preserved rejected comparisons

- `comparison-rejected-1/`: EWMA ran with 460 emitted originals, 441 SDK-proven
  completions and 19 exact known proxy refusals, no censoring or generator
  deficit. Baseline then attempted an unsupported mutable offload PATCH and
  failed before load. The whole campaign is invalid; ORACLE did not execute.
- `comparison-rejected-2/`: EWMA ran with 444 SDK-proven completions and 16 known
  refusals. Baseline ran with 434 SDK-proven completions and 26 known refusals,
  but the reader wrongly required an execution-node header that the standard
  API does not emit. The entire campaign is invalid. The corrected reader
  accepts missing baseline node metadata only with exact SDK identity and the
  frozen forwarding target; conflicting metadata still fails.

Every failed attempt retained observations and released its owned resources.
The pre-existing `nanofaas-stack` VM belongs to the operator and is preserved.

## Final comparison protocol

The seeded order is EWMA, baseline, ORACLE, each with fresh VMs and the same
frozen image/profile/qualification/trace/input/resource budgets. There is one
repetition, so no between-repetition confidence interval is claimed. Each mode
has 460 campaign originals in two 20-second windows (balanced 4/4 RPS, then
imbalanced 15/0 RPS); 40 warmup originals are accounted separately. Native modes
require acknowledged reciprocal one-hop plans. The baseline uses static full
replica budgets and native pressure routing with bounded edge sync queues.
All platforms explicitly select SYNC_QUEUE admission and advanced metrics.
Queue-disabled native edges still execute directly through the native route;
terminal cloud queues are enabled in every mode. Readiness and physical service
drift are checked from actual observations, not inferred from requested rates.

## Quality gates

NanoLab suite after the final fix pass: 4,134 passed, 3 skipped, coverage at the
existing 90% threshold (the earlier implementation gate passed 4,114 tests).
The suite includes installed-wheel execution outside the checkout. Types, Ruff
lint/format, configured Bandit, all five dependency contracts, and every CI
pre-commit hook pass. NanoFaaS default-module execution-runtime, offload and
control-plane tests pass for the terminal execution-ID correction. Their logs
are preserved under `quality/`.

## Historical bounded comparison before final review

| Mode | Planned/emitted | SDK completions | Known errors | Censored | Deficit | Physical local / peer / cloud |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| EWMA | 460 / 460 | 440 | 20 | 0 | 0 | 124 / 0 / 316 |
| Baseline | 460 / 460 | 431 | 29 | 0 | 0 | 431 / 0 / 0 |
| ORACLE | 460 / 460 | 460 | 0 | 0 | 0 | 253 / 12 / 195 |

All three runs conform and the frozen-identity campaign check passes. Actual
peer forwarding is proven in ORACLE; cloud forwarding is proven in both native
modes. Baseline cloud forwarding was not exercised in this bounded trace.
Every mode released its owned VMs/resources. These local counts verify the
workflows and are not a scientific performance ranking.

## Injected load failure

The public CLI `failLoad: true` run fails at the owned load task as intended.
Evidence records 460 planned originals, zero emitted, generator deficit460,
`valid: false` and `validComparison: false`; the original injected exception
survives. The owned k6 child exits after SIGTERM (`exitCode: -15`), the collector
closes, and every resource/VM release passes. Its raw evidence/report and
release journal are preserved under `load-failure/`.

Final independent `cleanup-inventory.json` lists only preserved external
`nanofaas-stack`; timestamped `cleanup-processes.json` contains no owned k6
generators. An earlier empty inventory capture was rejected as evidence and
replaced by this independently validated JSON capture.

## Final review status

The fresh review found that the historical qualification exercised forecast
inputs without actual handler contention. Its reported successful timings and
comparison counts remain credible bounded observations; they do not complete
B4. The old timing artifacts are now rejected by the comparison preflight.
A fresh stable-hardware calibration and loaded qualification have passed;
the refreshed comparison is documented below after verification. See
[final review](../final-review.md) and the execution ledger for all six fixes.

## Stable calibration and independently loaded qualification

`calibration-stable-rejected/` retains the first attempt that exposed an extra
environment field incompatible with the frozen profile schema. Its measurements
were not promoted into a profile. Runtime CPU observations are now a separate
immutable artifact; identity hashes stable hardware fields only.

`calibration-stable/` is an independent successful public CLI run: 144 direct SDK
samples, six capacity cells, D = 0.11153933683333334 seconds. Profile SHA-256:
`071763e44776537354ffdcba23cac010595e98207ac512a8e954f54c0f071a34`.
New environment fingerprint:
`sha256:468e9c7f1213d8be04db771e7b8a275bd3b0e52aa1fccae400a99f548b10dffc`.

`qualification-loaded/` runs owned paced HTTP load during preparation timing,
with independent SDK handler occupancy. Both cells independently qualify with
ten complete samples each, zero censoring, T = 20 seconds and lead = 1.9 seconds.
Worst-cell conservative q90 wall duration is 0.105648438 seconds. Timing SHA-256:
`c436311333813a472ecc6fb101f8b30050f3e19f5f81538adb7104d24d44e58c`.

| Cell | Measured originals / emitted / returned | SDK completions | SDK occupancy seconds | HTTP errors | Generator deficit |
| --- | ---: | ---: | ---: | ---: | ---: |
| Balanced 4/4 RPS | 1706 / 1706 / 1706 | 1432 | 164.223 | 274 | 0 |
| Imbalanced 24/0 RPS | 5121 / 5121 / 5121 | 4224 | 487.819 | 897 | 0 |

The audit covers the scheduled prefix through the final preparation observation
in each cell. The larger immutable schedule includes a tail outside that prefix;
its size is retained explicitly rather than called cancelled or measured. HTTP
errors are visible and are not called SDK completions. Maximum arrival lateness
is below 3 ms in each measured prefix. All owned generators, collectors,
containers and VMs were released. These local samples establish workflow
qualification, not a population tail bound or a scientific performance ranking.

## Refreshed comparison rejected for unresolved peer outcome

`comparison-loaded-rejected/` uses the new loaded qualification and stable
profile. EWMA conforms with 438 SDK completions and 22 known errors; baseline
conforms with 436 completions and 24 known errors. ORACLE emits all 460 campaign
originals, proves 459 completions and retains one censored outcome: epoch 1,
edge-0 original 22 receives a bodyless HTTP 502 from the peer path toward edge-1
after about 19 ms, with no returned execution identity. Its physical outcome
cannot be conservatively classified; the whole campaign is invalid. No precise
runtime cause is established by the available logs. All owned releases pass.
The independent repeat uses the same prerequisites and implementation and does
not remove this rejected attempt or establish universal reliability.

`comparison-loaded-rejected-2/` is that independent repeat. EWMA conforms with
440 completions / 20 known errors; baseline with 437 / 23. ORACLE proves 458
completions and retains two censored bodyless peer 502s (originals 127 and 277,
about 11 and 9 ms). All 460 originals per mode are emitted, without generator
deficit; every owned release passes. No further unchanged repeats are used.

Graph/source tracing shows that an unmarked remote 429 is retained in
`OffloadFailedException` but its message was removed by the origin controller's
bodyless 502. The d00d9f66 correction preserves the existing message in the
`OFFLOAD_FAILED` response body without changing quotas, routing, retries or
execution identity. NanoLab now accepts an explicit refusal only when its
message starts with the exact gateway target and remote 429 status; unrelated
failures remain censored. Previous ambiguous outcomes are not reclassified.

`calibration-peer-errors/` independently measures the new distribution: 144
samples, six capacity cells, D = 0.11159310559027778 seconds. Profile SHA-256:
`afd05d350c7803bc9bca3f0f426673e4e3c8f4550f110246448c580a4a685875`.
Fingerprint:
`sha256:44f9dc9960b0fa1fc8455806262c89398a8330d128989f7c03f4a522457ad20a`.
All releases pass. No former profile is silently migrated to the new source.

## Loaded qualification after remote diagnostics correction

`qualification-peer-errors/` independently qualifies the new d00d9f66 runtime:
20 complete samples across both matrix cells, zero censorship, T = 20 seconds,
lead = 1.9 seconds, worst-cell conservative q90 = 0.128882395 seconds.
Timing SHA-256:
`e646cb82262adfcbe9150dcc9f61cb5ed4e244ca4eaaf11888b6fc1226e112ce`.

| Cell | Measured originals / emitted / returned | SDK completions | Occupancy seconds | HTTP errors | Deficit |
| --- | ---: | ---: | ---: | ---: | ---: |
| Balanced 4/4 RPS | 1708 / 1708 / 1708 | 1438 | 165.054 | 270 | 0 |
| Imbalanced 24/0 RPS | 5127 / 5127 / 5127 | 4224 | 487.378 | 903 | 0 |

These are audited measured prefixes, with the unused schedule tail explicitly
retained outside that prefix. Maximum lateness is below 6 ms; all owned releases
pass. This qualification does not weaken the immutable capacity or protocol
limits and does not claim a population tail bound.

## Final qualified comparison with preserved remote diagnostics

`comparison-peer-errors/` passes with `validComparison: true`,
`workflowCompleted: true` and passed run metadata. All 144 tasks, including
resource releases, pass. Every mode uses source d00d9f66, profile afd05d35,
qualification e646cb82 and the same immutable trace/input/resource budgets.
Actual NanoLab package SHA-256:
`10376b76336ec9758848dac47f5830e863045c648e0e4063a74049a738d74722`.

| Mode | Planned / emitted | SDK completions | Known errors | Censored | Deficit | Physical local / peer / cloud |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| EWMA | 460 / 460 | 445 | 15 | 0 | 0 | 122 / 0 / 323 |
| Baseline | 460 / 460 | 437 | 23 | 0 | 0 | 437 / 0 / 0 |
| ORACLE | 460 / 460 | 458 | 2 | 0 | 0 | 245 / 10 / 203 |

ORACLE originals 82 and 172 retain the actual remote 429
`ONE_SHOT_ADMISSION_REJECTED` with reason `inbound assignment absent or quota
exhausted`. They are explicit pre-handler refusals, not SDK completions. This
confirms the observable native prerequisite without changing quotas/routing or
reclassifying any earlier ambiguous outcomes. SDK receipts independently prove
ten actual peer completions and terminal cloud completions in both native modes.
Baseline cloud fallback is configured and tested but not exercised in this trace.
All original counts are conserved and runtime assumptions pass.

Each mode also accounts separately for 40 warmup originals: all are emitted on
time and have returned HTTP outcomes. Warmup HTTP errors are EWMA 2, baseline 0,
ORACLE 3; those are visible HTTP outcomes, not claimed physical completions.
The campaign has one repetition, so a between-repetition confidence interval
remains unavailable. These local observations verify workflows; they do not
establish an algorithm ranking, a population tail or Azure performance.

## Final injected failure and independent cleanup inventory

`load-failure-peer-errors/` uses the latest pinned runtime/profile/qualification.
The public CLI exits 1 with the original injected RuntimeError, retains
460 planned / 0 emitted originals and deficit 460, and reports both physical
conformity and campaign validity false. The owned child is terminated/reaped
(exit code -15), the collector closes and every resource/VM release passes.

The independently captured `cleanup-final-inventory.json` lists only preserved
external `nanofaas-stack`. `cleanup-final-processes.json`, checked after all
workflows, records zero owned one-shot k6 generators. All original artifact
receipts can be verified after decompression. No Azure resources were acquired.
