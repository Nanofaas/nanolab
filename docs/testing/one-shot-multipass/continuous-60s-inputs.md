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
The updated manual logical observation mode accepts 60-second decisions with
the existing auction 60 s, preparation 10 s and lead 90 s settings. Qualified
and automatic modes retain their timing checks. Actual activation/expiry
deadlines remain in force; recorded maxima are not a promise that each epoch
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
dependencies, oracle and calendar. It records `timingPolicy=observe` and keeps
all prior resource and timeout values. No load or VM is started by preparation.

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
