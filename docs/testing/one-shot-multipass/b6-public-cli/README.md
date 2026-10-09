# Public CLI validation on Multipass

The three-node local topology has two edge VMs and one terminal cloud VM on a
shared ARM64 operator host. These are workflow validation results, not Azure or
scientific performance claims. The preserved raw evidence distinguishes SDK
handler executions, exact known proxy refusals, censored outcomes and generator
deficit. Missing telemetry is unavailable, not zero.

## Provenance and reproduction

The operator commands and YAML configurations are preserved alongside this file.
NanoFaaS runtime source: `3958d488a46bf608f749d9c1f673c984447c08a7`, whose terminal
execution-ID correction is based on `b1aa7f65c7ed0a70c2b94f37092fe62b7e877934`.
The distribution receipt pins the build recipe, source and image bytes. NanoLab
manifests additionally pin the actual package bytes used for each campaign;
pre-commit manifests honestly show a dirty source checkout.

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

## Calibration and qualification

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

NanoLab suite: 4,114 passed, 3 skipped, coverage at the existing 90% threshold.
The suite includes installed-wheel execution outside the checkout. Types, Ruff
lint/format, configured Bandit, all five dependency contracts, and every CI
pre-commit hook pass. NanoFaaS default-module execution-runtime, offload and
control-plane tests pass for the terminal execution-ID correction. Their logs
are preserved under `quality/`.

## Final observed comparison

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
