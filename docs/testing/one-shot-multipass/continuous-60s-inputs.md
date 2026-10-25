# Offline one-minute inputs for the continuous ORACLE experiment

`one-shot-rewindow.py` reuses the saved continuous arrival calendar and spline
coefficients. It integrates each minute to build a new oracle, without
resampling requests or starting a campaign. The observation workflow accepts the derived schema-v2 calendar while preserving
the v1/uniform paths. Runtime preparation is a separate explicit step.

Run with Python 3.12+ through the script's pinned NumPy/SciPy environment:

```bash
uv run --script scripts/one-shot-rewindow.py \
  --freeze /path/to/continuous-parent-freeze \
  --output /path/to/new-offline-inputs \
  --period-seconds 60 \
  --reference /path/to/original-python-model \
  --reference-python /path/to/original-model-environment/bin/python
```

`--reference` and `--reference-python` are supplied together. Omitting both
produces the trace/calendar/means only. An existing output directory is rejected;
a failed simulation retains its input and log without retry. Historical source
inventories and all pinned reference Python files are checked, and results keep
a hash inventory. No sampler, generator, VM or container is launched.

The original 54-minute measurement and 12-minute warmup keep the same offsets.
Fifty-four one-minute windows replace nine six-minute windows. The original
spline time scale is explicitly 360 seconds even though decisions use 60 seconds.

The mathematical means are saved in `means.json`. `means.csv` includes integrated
masses, actual integer counts and signed errors after nearest 0.05 requests/s
quantization, with midpoint ties upward. Rounding affects only ORACLE/model
input: it never alters the arrival calendar. Sub-quantum rates may round to zero.
The protocol quantum and eight auction rounds are inherited from the parent.

`selected.json` holds all original-model results, using the same independent
per-epoch local optimization and fixed-round convention as the earlier run.
`reference-environment.json` records the actual model interpreter/packages.
The preceding trace-only verification and final `oracle-verification.json` are
separate; `runtimeReady` deliberately remains false.

The prepared campaign bundle and readable routing/memory CSVs live in the
private `Nanofaas/dfaas-documents` repository, under
`experiments/one-shot-continuous-60s-2026-10-14/`.
The 14 October run kept auction 60 s, preparation 10 s and lead 90 s, which the
unpublished runtime it was built from (`c335eb9d`) accepted with 60-second
decisions. NanoFaaS main refuses them: the lead must be shorter than the period,
and auction plus preparation must fit in both. New minute freezes use auction
10 s, preparation 10 s and lead 30 s; in that run negotiation took at most
1.4 s and activation at most 4.8 s. Actual activation/expiry deadlines remain in
force; recorded maxima are not a promise that each epoch
will finish. The generator continues independently during negotiation.

Tests reuse the existing environments; no package installation is needed:

```bash
ONE_SHOT_CURVE_PYTHON=/path/to/scipy-environment/bin/python \
ONE_SHOT_REFERENCE_ROOT=/path/to/original-python-model \
ONE_SHOT_REFERENCE_PYTHON=/path/to/original-model-environment/bin/python \
uv run --frozen --all-packages --all-groups pytest \
  -c packages/nanolab/pyproject.toml \
  packages/nanolab/tests/one_shot/test_rewindow.py \
  packages/nanolab/tests/one_shot/test_reference_load.py
```

## Freeze and execute the observation

Prepare the updated recipe distribution from the matching clean NanoFaaS source
with the existing `assembleRecipe` task. Then use the existing preparation script:

```bash
uv run --frozen --all-packages --all-groups python scripts/one-shot-observe.py prepare \
  --tuning /path/to/one-minute-preparation \
  --arrival-profile /path/to/one-minute-preparation/arrival-profile.json \
  --parent-freeze /path/to/original-continuous-freeze \
  --observations /path/to/original-continuous-freeze/observations.json \
  --distribution /path/to/new-runtime-distribution/distribution.json \
  --source /path/to/new-runtime-source \
  --output /path/to/new-freeze
```

Preparation checks the parent's archived inventory independently of its retired
external checkout/environment. The new freeze binds the current runtime, package,
dependencies, oracle and calendar. Apart from the auction, preparation and lead
above, it keeps all prior resource and timeout values. Preparation and the run
check these three against NanoFaaS's configuration rules. No load or VM is
started by preparation.

One command then owns provisioning, collection, load, supervision, reporting,
archiving and cleanup:

```bash
uv run --frozen --all-packages --all-groups python /path/to/new-freeze/source-used.py run \
  /path/to/new-freeze /path/to/new-run --archive /path/to/new-run-archive
```

The saved entry point loads its frozen package. No agent polling or manual CPU
pinning is required. Progress is emitted on stdout and `status.json`. Raw epoch
negotiations/activations and `epoch-observations.jsonl` retain incomplete plans,
readiness lag and operation times; the run is not stopped by a missing auction
plan alone. Available plans still receive conservation, memory and topology
checks; confirmations involving a missing peer plan remain unverified evidence.
Owned process failures and SIGINT/SIGTERM still stop, archive and clean up.
Request rejects/errors remain separate measured results. Existing plan expiry
and routing/transition behavior is preserved; an expired plan is not extended
just to make an epoch look successful.

## The same minute run with a NanoFaaS load forecast

The one-minute observation can replace the ORACLE with one of NanoFaaS's own
load forecasts. Prepare it like the ORACLE run, adding `--forecast ewma`,
`--forecast holt` or `--forecast ari`:

```bash
uv run --frozen --all-packages --all-groups python scripts/one-shot-observe.py prepare \
  --tuning /path/to/one-minute-preparation \
  --arrival-profile /path/to/one-minute-preparation/arrival-profile.json \
  --parent-freeze /path/to/original-continuous-freeze \
  --observations /path/to/original-continuous-freeze/observations.json \
  --distribution /path/to/new-runtime-distribution/distribution.json \
  --source /path/to/new-runtime-source \
  --forecast holt \
  --output /path/to/new-holt-freeze
```

Each edge node then forecasts every function's external arrivals from complete
UTC-aligned windows of `--forecast-window-seconds`: by default 60 for EWMA, 10
for Holt and ARI. EWMA is an exponentially weighted moving average whose
`--forecast-alpha` (default 0.5) weights the latest window. Holt follows a damped
trend (`--holt-level`, `--holt-trend`, `--holt-damping`; defaults 0.4, 0.4, 0.95)
and ARI an autoregression of the window-to-window changes (`--ari-order`,
default 6), both over the latest 360 windows. Unlike EWMA, they forecast each
window up to the interval and answer the mean over it; NanoFaaS's
`docs/forecasting.md` defines them. The runtime needs Nanofaas/nanofaas#259.
The freeze records the settings in `operator-protocol.json` under `forecast`,
with a maximum age of two windows: NanoFaaS dates a forecast at the start of its
current window. Each auction freezes its forecasts when it starts, the lead
(30 s) before its interval. With 60 s windows the latest complete one then ended
30 to 90 s before the interval it predicts, depending on where the anchor falls
in the UTC minute; the interval just before is never complete. With the
14 October timing, each auction starting just after the previous activation, it
would have ended 70.75 s before. 10 s windows end 30 to 40 s before. The trace
is not uploaded to any node; it stays in the run only to measure the error.
Calendar, resources, timeouts, auction and lead are those of an ORACLE minute
freeze, and the manifest mode is the forecaster's name. Freezes prepared without
the option keep the ORACLE and their historical protocol unchanged.

Every observation run, whatever its forecast, ends with `forecast-errors.csv` and
`forecast-errors.json`, also after a failure that left `epochs.jsonl`. The CSV
has one row per interval, edge node and function: 54 x 5 x 5 = 1,350 rows. The
cloud runs no auction and has no forecast.

| Column | Meaning |
| --- | --- |
| `predictedRate` | `forecastRate` of the node's activated plan: the rate its auction used, in requests/s |
| `solverRate` | that rate floored to the flow quantum, as the solver received it |
| `actualRequests`, `actualRate` | requests the generator emitted from the node for the function in the interval, and that count per second |
| `scheduledRequests` | the calendar's requests for the interval, to separate generator lateness |
| `oracleRate` | the frozen trace rate the ORACLE run used |
| `error` | `predictedRate - actualRate`: positive overestimates, negative underestimates |
| `absoluteError`, `relativeError` | its magnitude, and the error over `actualRate` (empty when no request arrived) |
| `errorVsOracle` | `predictedRate - oracleRate` |
| `forecastSource` | `plan`, or `no-plan`/`no-epoch` when no activated plan recorded the forecast |

NanoFaaS reports the forecast only inside an activated plan, so a node whose
epoch produced none has an empty prediction; its arrivals are still counted.
The JSON summarises the errors overall, per node, function, node/function pair
and interval: mean signed error, mean absolute error, root mean square error,
and count, mean and largest of the overestimates and of the underestimates.

The same files can be computed from a finished run or archive, without VMs, into
a new directory:

```bash
uv run --frozen --all-packages --all-groups python scripts/one-shot-forecast-errors.py \
  /path/to/run-or-archive --output /path/to/new-forecast-errors
```

On the archived ORACLE minute run of 14 October 2026 this finds a plan for all
1,350 cells. `errorVsOracle` is zero throughout, and the error stays within two
requests per minute (±0.033 requests/s), with a mean absolute error of 0.0106.
That is the 0.05 requests/s rounding of the oracle against the integer arrivals
of each minute, so it is the baseline for the forecasters' errors. Two cells of the 1,350 differ by one request between
emitted and scheduled counts.

### Replaying the forecasters offline

The arrivals of a finished run can be replayed, without VMs, through Python copies
of NanoFaaS's EWMA, Holt and ARI as merged in Nanofaas/nanofaas#259. Each interval
is queried its lead before it starts, the run's own unless `--lead-seconds` is
given, and the results go to a new directory:

```bash
uv run --frozen --all-packages --all-groups python scripts/one-shot-forecast-replay.py \
  /path/to/run-or-archive --output /path/to/new-forecast-replay --lead-seconds 30 \
  --forecaster ewma:window=60,alpha=0.5 --forecaster holt:window=10 \
  --forecaster ari:window=10,order=6
```

`--forecaster provider:key=value,...` repeats; the keys are `window` (default 10),
`alpha`, `level`, `trend`, `damping`, `order` and `history`, with NanoFaaS's
defaults. Without it the replay uses EWMA with 60 s windows and alpha 0.5, EWMA
with 10 s windows and alpha 0.7, Holt and ARI. `forecast-replay.csv` has the
`forecast-errors.csv` columns preceded by `model`; `forecastSource` is `replay`,
or `no-window` before the first complete window. `forecast-replay.json` holds one
summary per model. The replay counts the generator's emitted requests, assumes each
function registered 60 s before the first arrival, and queries at the nominal lead
rather than at each auction's actual start. The copies agree with the Java classes
to within 1e-12, which a test pins.

On the archived ORACLE minute run with a 30 s lead, the mean absolute error is
37.3 requests per minute for EWMA with 60 s windows, 15.7 for EWMA with 10 s
windows and alpha 0.7, 7.5 for Holt and 8.6 for ARI, against a mean of 62
requests per minute per cell.

## Physical evidence collection after the minute-run diagnosis

The live collector preserves its pending-original order across collection slices.
Lookups visited in a slice move behind the unvisited originals, including when a
proof lookup returned 404 or its replica disappeared. A row whose lookup did not
start before the deadline remains at the front for the following slice. New k6
results are admitted once, and originals with released proofs leave the queue.
This prevents an unrecoverable old prefix from repeatedly consuming every slice.
The existing slice duration, HTTP limits, worker count and process-failure policy
are unchanged; no backlog threshold stops a campaign.

`evidence-collection.jsonl` records UTC time, visited originals, pending originals
and originals with a collected released proof after every completed pass. This
is diagnostic progress, not a count of failed requests or an acceptance gate.
Physical collection still cannot recover in-memory proofs that a removed replica
has already lost. All k6 request/response records remain the primary outcome
source; incomplete independent evidence remains explicitly incomplete.

The regression test exercises the real collector loop and bounded parallel lookup
with simulated HTTP and virtual time. It checks an irrecoverable prefix, fresh
proofs already available and proofs arriving during collection, and that expired
slices do not report lookups that never ran. Existing workflow tests cover Sonata
resource ownership, process faults, interruption and cleanup without VMs.

This change affects the next prepared package fingerprint. Previously frozen
packages and archived experimental results are not rewritten. The separate Loki
HTTP-500 delivery problem is not fixed by this collector scheduling change.

## Owned Loki ingestion health

The owned local backend now writes and queries a uniquely identifiable diagnostic
line after `/ready` first succeeds, then on the existing five-second supervised
service cadence. Its stable `nanolab_backend_probe` label is separate from node,
function and request logs. A failed push or missing query result is an operational
fault: no retry/restart, preserve raw data, release only owned resources, archive
and return failure. Request rejects are unrelated and remain experiment outcomes.
`logging/backend-probes.jsonl` records UTC and HTTP outcomes; a fault also saves
`backend-metrics-on-failure.txt`. A live container or `/ready` 200 alone is not
proof of successful ingestion. Every probe also requires a finite WAL failure
counter and zero recorded `loki_ingester_wal_disk_full_failures_total` errors.
Loki can swallow WAL ENOSPC while keeping logs queryable in memory, so a successful
write/query alone is insufficient. Missing, invalid or positive WAL failure
metrics stop the workflow; this is a real storage error, not a performance gate.

For the campaign-owned backend only, a saved Compose override explicitly sets
`-ingester.wal-disk-full-threshold=0` and keeps WAL explicitly enabled. Loki 3.7.8 otherwise rejects WAL pushes
once its filesystem occupancy reaches 90%, even if that shared host filesystem
still has substantial free space. Its error text is misleadingly identical to
an ingester shutting down. The override disables this percentage heuristic;
WAL persistence stays enabled, CPU/RAM limits are unchanged, and real write/read
failures and WAL disk-full errors remain fatal through the ingestion probe. External backends and the
shared deployment template are not modified. The generated override is archived
with the run and its policy is included in the next package fingerprint.

The retained historical run does not include the Loki disk gauge at fault onset,
so a disk-triggered rejection is a plausible mechanism, not a reconstructed disk
measurement for that run. An isolated fault injection reproduces `/ready` 200
plus push 500 with exactly that error when the WAL disk threshold is exceeded.
See the pinned primary sources:
[WAL policy](https://github.com/grafana/loki/blob/v3.7.8/pkg/ingester/wal.go) and
[push rejection](https://github.com/grafana/loki/blob/v3.7.8/pkg/ingester/ingester.go)
and [WAL ENOSPC handling](https://github.com/grafana/loki/blob/v3.7.8/pkg/ingester/instance.go#L247-L263).

A saved real-container smoke workflow reuses `backend_resource`, Sonata and the
existing supervisor with no VMs, functions or k6 traffic:

```bash
uv run --frozen --all-packages --all-groups python scripts/one-shot-logging-smoke.py \
  --runtime-source /path/to/matching/nanofaas/source \
  --host-address YOUR_EXISTING_PRIVATE_HOST_ADDRESS \
  --output /new/logging-smoke-directory
```

It requires the already cached pinned Loki/Grafana images, writes/query-checks
through background probes, stops writers before copying backend storage,
verifies the archive and removes owned containers/volumes. The output and sibling
`-archive` directory must be new. The private host address is explicit, respecting
the existing campaign endpoint validator. This diagnostic does not launch a
one-shot experiment. Simulated tests also execute the fault path through Sonata,
verifying first-fault cleanup and archiving without a restart.
