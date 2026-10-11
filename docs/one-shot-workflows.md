# One-shot workflows on Multipass

NanoLab provides three independent workflows: `one-shot-calibration`,
`one-shot-qualification`, and `one-shot-experiment`. Their bundled scenarios are
listed by `nanolab list` and can be copied from the installed package. Use the
bundled `multipass-one-shot.yaml.example` environment as a starting point.

These workflows currently qualify local execution on Multipass. The edge and
cloud nodes occupy separate VMs on the same physical host. Their manifests use
`provider: multipass` and `purpose: workflow-validation`; their measurements are
not Azure measurements or a scientific performance campaign.

## Prerequisites and immutable inputs

Use Python 3.12 or newer, Multipass, Docker/buildx and k6 on the operator host,
and a NanoFaaS checkout selected through `NANOFAAS_ROOT`. Each preset acquires
two edge VMs and one terminal cloud VM, with explicit CPU and RAM budgets.
The workloads use HTTP SDK execution with physical release proofs and one
active handler per replica. NanoLab owns its generator process, collectors,
clock monitors, function containers, control planes and VMs; Sonata releases
them in reverse order on success and failure.

The default JVM recipe includes `offload`, `forecasting`, `p2p`, `container-provider`
and `sync-queue`. A supplied `runtimeDistribution` reference must identify a
distribution built with the same recipe and modules. The terminal cloud
enables synchronous queuing in every comparison mode. The baseline additionally
uses native pressure-based offload on its edges, with bounded synchronous
admission depth equal to the maximum static replica count. Native one-shot
edges keep synchronous queues disabled. The admission profile is explicitly
`SYNC_QUEUE`, and `advanced` metrics expose per-function admission/rejection
counters. Baseline APIs can omit execution-node metadata: exact SDK execution
identity and the frozen forwarding target remain mandatory; a conflicting
metadata header invalidates the run. No images are implicitly published.

Copy the scenarios to a writable operator directory before editing them. Artifact
paths resolve relative to the scenario file. References contain a path and the
SHA-256 of that exact file, for example:

```yaml
profile:
  path: ./calibration/profile.json
  sha256: <SHA-256 of profile.json>
qualification:
  path: ./qualification/timing.json
  sha256: <SHA-256 of timing.json>
```

Compute hashes with `sha256sum`. The placeholder hashes in the installed presets
are deliberately unusable. Synthetic, missing, changed, wrong-provider or
wrong-purpose artifacts fail validation. Source, image, input, CPU/RAM,
co-location and environment fingerprints further restrict reuse. A changed
recipe or image requires explicit recalibration and requalification. No workflow
silently updates service time during a comparison.

## Run the three workflows

From a NanoLab checkout use the public launcher; an installed wheel exposes the
same commands through `nanolab`:

```sh
export NANOFAAS_ROOT=/path/to/nanofaas
./nanolab.sh run ./calibration.yaml --environment ./multipass-one-shot.yaml --run-dir ./runs/calibration
# Pin runs/calibration/profile.json in qualification.yaml.
./nanolab.sh run ./qualification.yaml --environment ./multipass-one-shot.yaml --run-dir ./runs/qualification
# Pin that profile and runs/qualification/timing.json in experiment.yaml.
./nanolab.sh run ./experiment.yaml --environment ./multipass-one-shot.yaml --run-dir ./runs/experiment
```

Use fresh run directories. Preserve the original scenarios, commands, source
commit, distribution receipt and artifact hashes together with the output.
The dossier in `docs/testing/one-shot-multipass/` records actual local runs.

Calibration measures warm physical handler occupancy independently of client
latency and queues. Raw samples, per-node confidence intervals and measured
capacity at one and two replicas accompany `profile.json`. Qualification triggers
native prepare concurrently on both edges under an owned paced workload, and retains
native events and wall-clock observations. `timing.json` selects the smallest
candidate period satisfying the measured quantile, sample count, censoring,
readiness and declared time budget. A failed qualification cannot start a campaign.

The experiment reuses the same trace, resources, coefficients and prerequisites
for baseline, ORACLE and EWMA. Its seeded order is randomized for each repetition,
with fresh topology between modes. The baseline starts the full declared static replica allocation and configures
the cloud target at control-plane startup; rejected edge sync-queue admission
falls back to terminal cloud. The function offload block is immutable and is
never changed through PATCH. ORACLE uploads the original trace rates; EWMA consumes
native observations. NanoLab triggers each epoch once and observes native
decisions through the public API; it does not implement the auction or solver.
The k6 schedule assigns original request IDs before dispatch and performs no
application retries or redirects.

## Read results and failures

Each mode directory retains `manifest.json`, `trace.json`, `schedule.json`,
`generator.jsonl`, SDK `physical.jsonl`, `runtime-inventory.jsonl`, native
`epochs.jsonl`, metrics and `evidence.json`. Trusted remote execution metadata
correlates the origin response with its terminal SDK execution proof. The origin
execution ID alone is insufficient for remote physical accounting.

`report.html` and `report.json` share the same measured data. The campaign produces
`comparison.html` and `comparison.json`, including best-effort reports on failure.
Charts show completed originals, known terminal errors, unresolved/censored
originals, generator deficits, physical routing, observed protocol wall/period
ratios, and available queue, handler and utilization metrics. JSON includes
latency quantiles, readiness transitions, model drift and uncertainty between
independent repetitions. Missing telemetry and a single repetition's confidence
interval remain explicitly unavailable.

A comparison is conforming only when all planned originals were emitted, every
original has a proven completion or known terminal error, physical attempts are
not duplicated, and runtime assumptions pass. Timeouts and bodyless gateway
errors without physical proof remain unresolved. A no-handler admission refusal
can be a known error, but never an invented physical completion. Service drift,
c1, container resource limits, transition RAM, readiness, one-hop destinations and
generator timing are checked separately. A failed run cannot be made conforming
by zero counters or missing evidence. `experiment.failLoad: true` injects a load
task failure to verify collection and cleanup.

For forwarded admission refusal, the runtime must preserve its `OFFLOAD_FAILED`
502 body. NanoLab requires the exact declared gateway target and remote 429
status in that message; a status mentioned inside another error's text is not
proof of refusal. Updating this runtime prerequisite requires explicit new
calibration and qualification rather than reclassifying old bodyless outcomes.

Realized utility is labeled with the final solver's zero-price convention:
`(alpha*local + delta*peer - gamma*(cloud+errors))/planned originals`. Native plans
do not expose monetary bid transfers, so this quantity excludes those transfers
and is unavailable for nonconforming runs. Client latency includes terminal
queues and RTT; calibrated service time does not. Lower throughput or utility is
an observed outcome, not itself a qualification failure.

## Next: Azure

Azure remains a separate phase: configure and verify the target provider and
network, measure a new physical service profile, independently qualify auction
timing/readiness, then design and run the scientific campaign with sufficient
independent repetitions. Multipass artifacts cannot qualify that environment.
No Azure credentials are included in exported local manifests.

## Validation dossier

The checked-in [Multipass dossier](testing/one-shot-multipass/b6-public-cli/README.md)
records actual public CLI commands, immutable receipts, successful and rejected
qualification attempts, raw request/SDK observations, cleanup journals and the
local comparison results. Its scope remains workflow validation. Phase C needs
target Azure provider/resource verification, fresh calibration and qualification,
and an independently designed repeated scientific campaign.

Final review hardening binds service drift to each immutable profile tolerance
(an explicitly frozen stricter threshold is allowed), checks warmup arrivals and
HTTP outcomes separately, and includes workflow failure in comparison validity.
Stable CPU identity hashes model/features/topology; frequency/BogoMIPS/clock
observations are recorded separately. Old hardware fingerprint formats require
explicit recalibration. Forecast-only timing artifacts are rejected: each matrix
cell must include accounted emissions and exact SDK occupancy under actual load.

## Heterogeneous scientific campaign

Use the packaged `one-shot-heterogeneous-calibration`, `qualification`, and `pilot`
scenario templates for five different edges and a terminal cloud. They require
real immutable artifact references and a measured capacity matrix; their zero
hashes are intentional blockers, not runnable qualification. See
[the campaign dossier](testing/one-shot-heterogeneous-multipass/README.md) for the
resource table, sparse graph, acquisition gates and conditional paired analysis.
`freeze_campaign_protocol` and `write_campaign_scenarios` emit standard public
experiment scenarios; no separate infrastructure orchestration or CLI is needed.
Scientific fingerprints include the installed NanoLab code/assets and explicit
control-plane limits; changing either invalidates calibration compatibility.

### Concurrent multi-function capacity

Strict multi-function calibration uses the version 2
`measured-concurrent-capacity` artifact. Each record binds a node, the complete
replica vector, per-function rates and three relative SHA-256 references to raw
`trial.json` files. Reading the profile verifies those bytes, physical completion
accounting, lateness and service drift again. A scalar legacy capacity cannot
qualify multiple functions. Isolated rates never qualify an unmeasured mix.

The sweep predeclares every integer replica level for each function and batches
nodes at the same level. Mixed trials use a memory-feasible balanced vector and
five dominant-function vectors, with three additional simultaneous repetitions
at the selected rates. All attempts remain in `capacity.jsonl`; a failed gate
does not publish a qualified profile. `capacity-plan.json` records the factors,
vectors and minimum acquisition time before the first capacity trial. Replica
transitions, warmup, evidence collection and infrastructure add to that time.

The five-function design with maximum replica counts 5/5/10/10/15/35 requires
3,276 trials at 90 seconds: at least 81 hours and 54 minutes for this exhaustive
calibration. The synchronized experiment itself measures nine 360-second
intervals, or 54 minutes per mode, across all six nodes simultaneously. Each mode
also requires at least 12 minutes of warmup. Pilot and main repetitions are
separate acquisitions, with their full time reported separately.

### Synchronized multi-function templates

The packaged `one-shot-multifunction-calibration`, `qualification` and `pilot`
templates bind five distinct Rust handlers, strict 64 MiB replica limits and the
two triangles sharing edge-3. Zero artifact hashes and provisional qualification
rates deliberately block acquisition until measured artifacts replace them.

The candidate trace has nine 360-second windows: control, five distinct node
hotspots, two triangle hotspots, diffuse overload on edges 1/3/5 and recovery.
Every node/function cell remains positive. Rates come from a complete balanced
concurrent-capacity record, rather than assembled isolated maxima. The cloud
keeps a feasible balanced replica vector. Each of the nine worst-case aggregate
cloud mixes requires three additional 90-second trials and at least 10 percent
per-function headroom. These 27 trials add 40 minutes 30 seconds to the capacity
acquisition; the combined minimum is 82 hours 34 minutes 30 seconds.

Timing qualification checks SDK readiness and physical cgroups for all cloud
replicas before sampling the complete trace. Fifty samples for each of nine
cells, at 360 seconds plus a three-second measurement lead, require at least
45 hours 22 minutes 30 seconds. Provisioning, evidence collection and cleanup
add further time. These calibration costs are distinct from the 54 measured
minutes of each experiment mode. All acquisition attempts and cleanup errors
remain in the evidence, including attempts that cannot publish a qualification.

Version 2 paired campaign protocols freeze all nine epoch indices and five exact
function aliases. The public scenario writer emits nine one-epoch windows and
720 seconds of warmup. Changed analysis, aliases, runtime settings, installed
code, seed or load fail before resources are acquired. Every planned edge
original enters the paired denominator; cloud-direct requests are separate.
Reports expose every physical cell and summaries by function, node and epoch,
using a complete balanced capacity record as a descriptive reference. No
steady-state claim applies to the nine changing conditions. Legacy version 1
protocols preserve their original interpretation.

### Whole-VM and container telemetry for the synchronized campaign

Strict multi-function campaigns collect one sample per second on **all five edges
and the cloud**. A small VM-local Python sampler reads `/proc/stat`,
`/proc/meminfo` and Docker cgroupv2 counters. It starts before k6 and remains active
through the 720-second warmup and all nine 360-second epochs. Fluent Bit/Loki
continues to collect application logs; it is not used as the resource evidence
source.

`rep-*/vm-resources/<node>.jsonl` contains UTC timestamps, monotonic time,
run/mode/phase/epoch and function scope. VM CPU utilization is a fraction of the
node's vCPU capacity (`1.0` means all vCPUs busy); used cores, iowait and steal
are reported separately. Guest time is not counted twice. Memory total,
available and used, and swap total/free/used are bytes. Used RAM is
`MemTotal - MemAvailable`, not `MemTotal - MemFree`.

Each sample also contains every running Docker container, its ID and PID start
time, role (`control-plane`, `collector`, `function`, or `other`), function and
replica. Container CPU is reported in used cores, VM fraction and quota
fraction; memory current/peak/limit, swap and memory events are physical cgroup
values. Throttling includes cumulative periods/count/microseconds and interval
deltas. Restarted containers start a new counter baseline. The sampler's own
CPU time and peak RSS are recorded separately. The VM measurement covers kernel,
Docker, OS and observer work as well as the workload; control-plane metrics
cannot substitute for it.

Multipass stdout and stderr use pipes, drained concurrently into unbuffered
owned host files: direct regular-file output can stall the Multipass transport.
Drain failures invalidate collection. Transports are reaped and both pipes are
drained before raw files are synced and closed, retaining terminal partial lines.
The frozen telemetry policy also identifies this transport.

Raw streams and stderr remain on the campaign host even when load or cleanup
fails. `policy.json` binds the exact sampler SHA; `coverage.json` records raw
SHA-256, expected/observed seconds, missing and duplicate seconds, unavailable
CPU and collection/container errors for every node. Missing values remain
unavailable, never zero. An incomplete whole-VM scope invalidates scientific
run evidence. Reports show per-node CPU/RAM/swap and per-container CPU/RAM/
throttling, with gaps and epoch boundaries; they also read raw files in failed
runs without `evidence.json`. Calibration capacity trials and timing prefixes
use the same sampler after its overhead gate passes.

Before scientific VM acquisition, set
`oneShot.observability.vmResourceOverhead: {path: ..., sha256: ...}` to a verified
physical gate artifact. It must contain three complete paired observer-off/on
trials with logging enabled on both sides, every node/function cell, <=5%
goodput loss and p95 increase, verified raw observer receipts, matching resource
and input scope, sampler SHA and NanoLab package fingerprint. The logging
observer gate remains separate. Gates are not service calibration or timing
qualification; administrative bootstrap bypass exists only in the internal
Sonata topology builder, not in campaign YAML or public CLI options.

The instrumented capacity path reserves a 45-second pre-load lead to start all
six streams before the first scheduled request. Across 3,303 trials this adds
41h17m15; capacity load plus these leads totals at least 123h51m45. Runtime
preparation, streaming cleanup and provisioning add further time. The nine
360-second campaign epochs and its separate 720-second warmup are unchanged.

### Returned errors and request denominators

Each run report reads the immutable schedule and `generator.jsonl`, including
when the workflow failed before producing `evidence.json`. `returnedResponses`
counts original requests once, separately for warmup and measured campaign.
It reports totals and counts by origin node, function, epoch and their combined
cell, for each mode. Cloud-direct requests are included in these descriptive
totals; the primary scientific edge-only throughput remains separately defined.

Rejects (HTTP 429 and the platform's explicit wrapped admission refusals), other
HTTP errors, application errors returned with HTTP 200, HTTP timeout responses
(408/504), and invalid responses are mutually exclusive. HTTP timeout responses
count as returned errors but do not prove whether execution completed. Counts and percentages use explicitly named planned
and emitted denominators. Zero denominators produce `null`, not a zero error
rate. Timeouts, missing results, requests never emitted, duplicate results and
unexpected originals are visible separately. Conflicting duplicate responses
are unresolved; identical duplicate log events do not inflate counts. A generic
503 is an HTTP error, not sufficient evidence of a rejection.

The report includes up to 100 diagnostic response records with their original
status, body, timestamps and correlation fields; every response remains in the
raw generator artifact. An HTTP/application success is descriptive response
evidence: physical handler completion and correct function output still require
the existing independent proof and checksum validation. Returned-error rates do
not make an otherwise censored or incomplete run conforming.

The same explicit-rejection classifier is shared with scientific conservation:
Rust `RUNTIME_HANDLER_SATURATED` / `RUNTIME_CALLBACK_SATURATED` refusals wrapped
inside a control-plane `EXTERNAL_ERROR` remain rejects, even with HTTP 200.
An `EXTERNAL_TIMEOUT` response counts as a returned timeout error and does not
establish execution completion. Parse structured codes, not vague substrings in
an error message; a generic external error remains unresolved scientifically
until the independent terminal proof is available.

Clock monitoring measures all six VM probes in scientific calibration, qualification and experiments. Skew and measurement uncertainty remain mandatory for the terminal cloud. Health publication targets only edge negotiation APIs: the cloud intentionally disables them. A failed edge publication remains fatal; a missing cloud API is never used to bypass a failed clock measurement.

The multi-function Multipass presets round each function CPU quota down to a multiple of 0.01 CPU before freezing: 0.10 on edge-1/2, 0.14 on edge-3/4, 0.16 on edge-5 and 0.21 on cloud. runc's systemd driver rounds fractional percentages upward ([implementation](https://github.com/opencontainers/cgroups/blob/main/systemd/common.go)); a declared 0.149 CPU was observed as `15000 100000` in `cpu.max` during the v4 bootstrap. These representable limits preserve the 0.50 CPU control-plane/collector/OS reserve even at the maximum memory-feasible replica count. Strict cgroup validation is unchanged: Docker declarations and kernel limits must match, and all service/capacity profiles must be measured at these final quotas. Failed preflight evidence is retained.

Oracle rates use decimal quantum multiplication before JSON serialization. The runtime requires an exact decimal grid, so float multiplication tails such as `19 * 0.05 == 0.9500000000000001` are invalid. Oracle export rejects those off-grid values rather than treating them as equal within a tolerance; generated rates serialize as `0.95`. Parallel epoch preparation retains HTTP refusal status and response body in raw timing observations, marks the sample censored and never retries the trigger.

The strict five-function recipe reserves eight P2P transport slots: four for
endpoint advertisements at the central node and four for concurrent auction
requests. Both activities share the same bounded transport semaphore. The
default four-slot budget can reject auction traffic when advertisements overlap.
This resource setting applies equally to observer variants and scientific modes;
auction deadlines and acceptance thresholds are unchanged. Rebuild the runtime
distribution and freeze its updated recipe hash before acquiring new data.
