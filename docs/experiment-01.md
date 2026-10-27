# Experiment 01: control-plane cost in front of one function

`compare` measures the working set and CPU of the k3s control plane in front of a
single `qr-code-rust`, across seven targeted builds and three interleaved
repetitions. The provisioning, prepare/publication, immutable distributions,
manifest, resume and evidence of the historical comparison are reused; what is
specific to this experiment is the `experiment-01` load profile, its recipes and
its outcome contract.

**Status: implemented and verified locally; not run on Azure.** Native builds
need a nanoFaaS revision with `build.native.maxHeap` (below). No experimental
result, native support or capacity threshold follows from the local tests.

## Builds

The keys are the recipe basenames in `assets/presets/recipes/`. Core is
implicit. Every JVM recipe sets `-XX:+UseSerialGC -Xmx64m` plus its tier; every
native recipe sets the serial collector and `maxHeap: 64m`. Every recipe
includes `build-metadata` and none carries a `variant` label.

| Key | Modules beyond core | Runtime |
| --- | --- | --- |
| `experiment-01-core-k8s-jvm-jit` | k8s-deployment-provider, build-metadata | tiered (C1→C2) |
| `experiment-01-core-k8s-native-o3` | k8s-deployment-provider, build-metadata | native -O3 |
| `experiment-01-core-k8s-queue-jvm-int` | + async-queue | interpreted (`-Xint`) |
| `experiment-01-core-k8s-queue-jvm-c1` | + async-queue | C1 (`TieredStopAtLevel=1`) |
| `experiment-01-core-k8s-queue-jvm-jit` | + async-queue | tiered (C1→C2) |
| `experiment-01-core-k8s-queue-native-o2` | + async-queue | native -O2 |
| `experiment-01-core-k8s-queue-native-o3` | + async-queue | native -O3 |

`build-metadata` is in every build, so the composition stays uniform: nanoFaaS
requires it for the explicit -O2 (`RecipeBuildProperties.requireBuildMetadata`),
and before the load each cell reads `/modules/build-metadata` from the running
control plane, keeps it as `build-metadata.json` and stops if its modules or
optimisation level differ from the recipe. Its footprint is part of every
measurement.

The runtime class (`interpreted`, `c1`, `tiered`, `native-o2`, `native-o3`) is a
field of the build, separate from its key. `jvm_optimization()` refuses these
builds rather than labelling `-Xint` as C2.

Only the first recipe declares `qr-code` (Rust); it is published first and every
cell uses that one function digest.

### Native heap

A native control plane takes its 64 MiB limit at build time:
`build.native.maxHeap: 64m` becomes `-R:MaxHeapSize=64m`, the image's default
maximum heap, so nothing is passed when the container starts. The distribution
report carries the value, and `compare` requires it to match the recipe before
the load. The running heap is observable in `scheduler-metrics.txt`
(`jvm_memory_max_bytes{area="heap"}`) but is not checked.

The field is in nanoFaaS `main` since Nanofaas/nanofaas#256, and in the CI pin.
An older checkout's schema rejects it, so the native builds fail to prepare
there; the operator's checkout must include #256.

## Protocol

The scenario's `experiment01` block (`config/experiment_01.py`) holds every
parameter; none is a matrix axis. The calendar is idle, warmup, plateaux at
constant arrival rate with sharp changes, then a bounded drain. The validator
requires positive integer arrival rates (including warmup), and rejects
non-increasing rates, non-finite or non-positive values,
`gracefulStopSeconds < httpTimeoutSeconds + completionMarginSeconds`,
`drainSeconds < gracefulStopSeconds`, fractions outside `[0, 1)`, and a
generator window longer than a plateau. `status: frozen` requires the generator
fidelity thresholds; a pilot may leave them open, and then no step is ever
reported as delivered.

The scenario must declare explicit requests and limits for both `control-plane`
and `qr-code-rust`; the container limits are separate from the 64 MiB heap. The
payload is `payloads/experiment-01-qr-1024.json` (`"A"` × 1024, `size: 1024`),
validated against its file sha256. Each invocation sends compact JSON
`{"input": <payload>}`. The protocol hash and the identity's `payload.sha256`
and `payload.bytes` describe the unchanged file; `payload.bodySha256` hashes
exactly the UTF-8 bytes sent in `E01_PAYLOAD`. Experiment 02 uses the same
wrapping helper. Manifests without the sent-body hash and receipts from those
identities cannot be resumed: keep them intact and use a new run directory.

`experiment-01-pilot.yaml` holds provisional values. `experiment-01.yaml` is
marked `frozen` but **still carries the pilot's values and placeholder
thresholds**; replace them with what the pilot confirms before the campaign.

## Order

Three orders, A–G being the table rows: `ABCDEFG`, `DGBFAEC`, `FCEAGDB`. Each
build takes three different positions; this reduces, but does not remove,
confounding with position and predecessor. The pilot uses the first order;
further repetitions cycle. A subset keeps the orders filtered and is recorded as
`subset: true`. The effective order is part of the manifest identity; a change
requires a new run directory.

## Generator and ledger

`assets/k6/experiment-01.js` runs one `constant-arrival-rate` scenario per load
phase with explicit `startTime` and `gracefulStop`, and a sending-nothing drain
scenario. It writes events through `K6_CONSOLE_OUTPUT` to `k6-events.jsonl`: a
`send` immediately before `http.post` (with the phase captured then) and one
terminal `done`, `timeout` or `error`. `done.ok` is the light check: nanoFaaS
envelope, success, `image/png`, base64, PNG signature and IHDR at least the
requested size. Malformed JSON/envelopes and decoding errors still produce a
`done` event with `ok: false`, so completed incorrect responses are never counted
as pending. It does not decode the QR symbol.

Per-scenario `dropped_iterations` and `http_reqs` reach the summary through
always-passing thresholds. A missing per-phase dropped metric is reported as
missing, not as zero.

## Outcomes and resume

Each attempt writes into `<build>/run-<n>/attempts/<k>/`. A cell is complete
only when `<build>/run-<n>/cell-result.json` names a definitive outcome and the
hashes of its evidence still match:

- `measured`: the run completed; requires the summary, events and snapshot. It
  says nothing about sustainability.
- `oom`: a control-plane container was OOM-killed, before or during the load.
- `startup_failed`: before the load, the control plane crashed instead of
  becoming ready.

Failures are committed with `diagnostics/` (pod listing, error) and the list of
artefacts that could not be produced; they are never retried. A transport
failure before the load, with the control plane running and ready, is retried
once in a new attempt. An error after `load-started.json`, or one that cannot be
classified, stops the matrix with its partial evidence; the next resume starts
a new attempt. Previous attempts are never overwritten.

The remote load generator also uses a distinct directory for each attempt,
under `~/nanofaas-loadtest/recipe-<run-path-hash>/`. The event ledger is fetched
when k6 exits, including failure or cancellation, before the summary is fetched.
If that transfer fails, the next attempt leaves the remote files intact and the
error records their location for recovery. Remote attempt files remain available
until explicitly removed by the operator.

With `--fresh`, starting a replacement moves the selected receipt into its old
`attempts/<k>/cell-result.json`, preserving that receipt and its evidence. The
cell stays incomplete until the new attempt commits a definitive outcome; an
interrupted replacement is resumed even when an older attempt succeeded.
Diagnostics also retain evidence-recovery error notes and remote paths when
fetching the generator ledger fails.

## Verdicts

`experiment-01-summary.json` and `.csv` hold one row per cell and phase, with
three distinct fields:

- **load**: `delivered`, `unverified` (pilot) or `not qualifiable: generator
  limit`, from the frozen thresholds: deviation over the whole step and over
  each window, dropped fraction and start delay, measured from the calendar
  origin. Missing metrics, truncated events or ledger anomalies also disqualify.
  Observed measurements are kept either way.
- **sustainability**: only for delivered steps: correct-within-threshold over
  **all requests sent by the cohort** and pending fraction.
- the attempt's **outcome**.

Requests belong to the phase that sent them. Latency percentiles are over the
completed responses of the cohort. Working set and CPU are summarised per phase
with the delta from idle; a CPU point whose 30 s rate window crosses into the
previous phase is excluded; the query step is 5 s. Missing series are `null`.

The summary does not yet apply a queue-trend criterion, does not draw time
series charts, and does not separate the control plane from the function per pod
identity beyond the existing cAdvisor queries.

## Running it

```bash
export NANOFAAS_ROOT=/path/to/nanofaas
E01=experiment-01-core-k8s-jvm-jit,experiment-01-core-k8s-queue-jvm-int,experiment-01-core-k8s-queue-jvm-c1,experiment-01-core-k8s-queue-jvm-jit

./nanolab.sh compare experiment-01-pilot.yaml \
  --environment /path/azure-comparison.yaml \
  --variants "$E01" --repetitions 1 --run-dir /path/runs/experiment-01-pilot
```

Without `--variants`, an experiment-01 scenario selects all seven builds, which
needs that nanoFaaS revision. The environment must be Azure with
exactly the `stack` and `loadgen` roles. The VMs are kept after the run, as for
every `compare`; the D8s_v5 stack is sized for native builds, not by the
footprint being measured.

There is no `compare` teardown. The provider's teardown destroys a VM from its
local `~/.azure-vm-sdk/<name>` workspace; if that workspace is missing, list what
is left with `az resource list -g <group> --query "[?starts_with(name,'<name>')].id" -o tsv`.

## Local verification

- Protocol, scenario, recipes, matrix orders, receipts, failure classification,
  ledger, generator gate, manifest identity and the cell runner:
  `tests/config/test_experiment_01.py`, `tests/comparison/test_experiment_01.py`,
  `tests/cli/test_comparison.py -k experiment`.
- k6 v1.0.0 smoke against a fake nanoFaaS endpoint: a response completing about
  34 s after its phase ended stayed in its cohort, a 45 s client timeout was a
  `timeout` terminal, and the delivered steps passed the gate. Killing k6 with
  SIGTERM left a readable events file with the in-flight request pending.
- The installed wheel, outside the checkout, resolves the seven recipes, both
  scenarios, the payload and the script.

Not established locally: native builds, a k3s deployment of either composition,
the observed flags and heap of a running control plane, full QR decoding, C2
compilation during warmup, isolation and saturation.

## Azure SDK 0.7.0

Every call the Sonata Azure provider makes (`AzureClient(...)`, `get_vm`,
`launch`, `ensure_running`, `wait_for_ip`, `close`, `delete`, `exec_structured`,
`transfer`, `VmNotFoundError`) keeps its signature in azure-vm-sdk 0.7.0; the
changes are additive (Spot parameters, `launch_many_iter`) plus orphan-disk
cleanup in `delete`. Sonata 0.6.16 widened its range to `azure-vm-sdk>=0.6.2,<0.8`;
nanolab pins `sonata-engine`/`sonata-tasks` 0.6.16 and `azure-vm-sdk` 0.7.0.
