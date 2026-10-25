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

Function CPU quotas are per-container upper limits, not dedicated CPU
reservations. NanoLab preserves the configured quotas when the function memory
pool changes and allows their sum across replicas to exceed the VM's vCPU
count, including when logging is enabled. Busy containers then share the
available CPU. Memory-pool admission and actual per-container CPU/RAM checks
remain enforced. Record VM CPU utilization, container throttling and execution
times to assess contention after the run; CPU contention calibration is not a
prerequisite for the first functional experiment.

The five-function presets use memory limits of arithmetic64, sort128,
memory256, matrix128 and mixed256 MiB. Pools are 832/832/1664/1664/2496/5824 MiB
for edge-1 through edge-5 and cloud, preserving 1/1/2/2/3/7 initial replicas of
each function. Individual replica maxima follow `floor(pool / function RAM)`.
To prepare a new copy from an earlier scenario with the existing balanced vector:

```sh
python scripts/one-shot-function-memory.py --input original.yaml --output prospective.yaml
```

The script validates both configurations, keeps CPU quotas, workload inputs and
VM dimensions, and records source/scenario/script/package hashes in
`prospective.memory.json`. It refuses existing output files and provisions no
VMs. The receipt is unqualified and marks load tuning as pending. Earlier service
observations retain their original 64-MiB scope; this configuration change does
not turn them into newly measured service times.

## Tune the first functional load with the Python reference

The saved `scripts/one-shot-tune-load.py` coordinator uses the existing NanoLab
environment and invokes cases in the isolated DFaaSOptimizer environment pinned
at `71899f720a4ffffd070ebf7ddc5b74afddfde9c5`. It verifies every Python source
against its archived bytes and records interpreter/dependency versions. Reuse
the prepared reference and the provenance-labelled observed service estimates:

```sh
python scripts/one-shot-tune-load.py tune \
  --scenario prospective.yaml \
  --observations observed-demands.json \
  --reference /path/to/preparation/reference \
  --reference-python /path/to/preparation/python-env/bin/python \
  --output /path/to/new-load-tuning
```

This runs the original LSP/LSPr_x solver and bid/grant/memory-start helpers for
exactly eight rounds, with aggregate bids, epsilon0.01, eta/zeta/latency/fairness
weights zero, beta1 and target utilization0.9, matching the base auction options.
There is no convergence claim, assignment replacement, hierarchy, VM deployment
or performance qualification. The cloud is outside the edge auction and retains
seven replicas per function in the current layout.

The sweep tests factors1.5/2/3/4/6/8/12/16 relative to balanced-vector capacities.
Five single-edge hotspots emphasize different functions; two further conditions
combine hotspots in both triangles and on E1/E3/E5. Opening E2/arithmetic and
closing E5/sort replace the earlier quiet control/recovery windows, following
the October12 request for horizontal offload in every interval. Select the
first tested factor with actual neighbor grants; the seven central windows
also require residual load requested at cloud. The complete selected trace must
have peer offload in all nine windows, with one source sending to at least two
distinct receiving edges in at least two windows. Count destinations across
functions: multiple functions sent to one edge count as one receiver. Cloud
traffic is separate. `offload-coverage.json` lists every directed peer route,
its per-function rates and the multi-receiver origins. If no candidate fits,
stop and retain diagnostics; do not silently substitute a failing pattern.
These are workload choices, not acceptance thresholds for the subsequent
measurements.

Every candidate retains exact inputs, eight-round transcripts, replica vectors,
local/neighbor/cloud-requested flow and command diagnostics. The selected shared
trace has thirty positive rates per window on the0.05 requests/s grid, nine
360-second periods and54 minutes planned across all six nodes simultaneously.
The export includes CSV, a prospective scenario and temporal plots. Output
directories must be new; the first failed external step stops the sweep without
retry. Future runs execute the saved source snapshot to preserve its fingerprint.

## First unqualified ORACLE observation

`scripts/one-shot-observe.py` prepares and executes the first functional run.
It reuses Sonata's owned VM/platform/logging lifecycle, the shared k6 ingress
schedule, native public one-shot APIs, physical proof collection and continuous
whole-VM/container telemetry. It does not fabricate capacity or timing
qualifications and does not enter the public qualified comparison workflow.

```sh
python scripts/one-shot-observe.py prepare \
  --tuning /path/to/load-tuning-02 \
  --observations /path/to/observed-demands.json \
  --distribution /path/to/distribution.json \
  --source /path/to/nanofaas-checkout \
  --output /path/to/new-freeze
python /path/to/new-freeze/source-used.py run \
  /path/to/new-freeze /path/to/new-run --archive /path/to/permanent-archive
```

Preparation copies the exact package, script, recipe, runtime descriptor, trace
and observations and hashes all saved bytes. Execution imports the captured
package, checks dependencies/host boot identity and rejects drift. Every node
keeps its approved CPU/RAM quotas, balanced initial vectors and Rust workload.
VM bootstrap waits for successful `cloud-init` before package installation;
the owned run retains the original status output.

Run directories may be relative to the operator's current directory and may
contain spaces. The shared generator resource resolves its directory once when
the workflow is constructed, then passes an absolute schedule filename to k6.
This is necessary because k6 resolves JavaScript `open()` paths relative to the
script, rather than the operator's directory. No machine-specific path is
embedded in the package. Regression tests initialize the real k6 module through
Sonata using relative and absolute directories, including a later cwd change;
`k6 inspect` sends no HTTP requests. A corrected package requires a new freeze;
existing freezes and failed-run archives remain unchanged.

Profiles retain the historical sample counts, means and p95; deviation is
computed from the same deduplicated physical samples, whose hash is verified.
The native contract uses `synthetic=true` and `workflow-validation` to mark
prospective RAM/replica adaptations as unqualified, with the original data scope
retained alongside the profiles. These are observed estimates, not invented
service times or qualified capacity curves. Deployment still uses the strict
resource settings; that adapter tag is not a statistical claim.

Before warmup, a 60-second idle collection records sampler/collector CPU and
memory cost, retaining all raw counters. It does not estimate the paired effect
on function goodput/latency and has no 5% acceptance gate. The nested
`observer-precheck/manifest.json` declares `probeScope: parent` and
`probeParentLevels: 1`, so temporal correlation reads the main run's clock
probes. Reanalysis of a failed run must use a separate derived directory;
retain the original manifest and raw samples unchanged. The run then covers
720 seconds of warmup and nine simultaneous 360-second windows:54 minutes of
measurement. All originals go through the NanoFaaS ingress; bootstrap warm
invocations are separately labelled SDK checks. Error/reject counts and ratios
use the preserved scheduled/emitted denominators, by phase/node/function/epoch.

The declared arrival-lateness limit is3600 seconds. Both the operational and
auction fractions are0.20, the maximum admitted by the pinned native runtime.
Preparation freezes both values; execution validates them and the declared
operational budget before inventory, workflow construction or VM acquisition.
The functional observation overrides physical preparation to 10 seconds as
requested by the user on 13 October. The 4-second auction plus 10-second
preparation require a 14-second lead; the protocol and runtime manifest use
the same derived lead. Previous freezes retain their original values. The sum
fits the period fraction. The first attempt's invalid0.99 settings remain in its
original archive; a corrected run requires a new freeze.
`RunOneShotLoadTask(enforce_timing_budget=False)`
records complete slow epochs in this observation; censored/missing preparations,
invalid ready plans, transport/process/collector failures still stop execution.
The qualified comparison path keeps its default timing enforcement. Legacy
comparison diagnostics remain visible in the unqualified report and do not
turn measured performance into an acceptance gate. Known swap-limit loss is
explicitly allowed; actual RAM/CPU/OOM controls and raw swap values remain.

Output directories must be new. The first operational failure stops the owned
workflow without retry; raw artifacts are copied and hash-checked into the
requested permanent archive after resource cleanup.

The load task declares telemetry directly in its `requires`, alongside the
generator, collector and topology resources. Sonata limits task access to direct
declarations even when a resource is already acquired through a prerequisite.
The observation integration test executes the load task through Sonata with
simulated resources and the compiled workflow's actual dependency list; it
checks task access and cleanup before any VM acquisition. Compilation alone
does not exercise this resource boundary.

The cloud capacity assessment is separate from actual acceptance: retain excess
load as a prediction, not observed rejects or successes. Historical64-MiB
service estimates remain unqualified for different replica vectors and RAM
limits. The static model does not simulate CPU contention, carried queues,
network delays or cold starts; physical saturation, completion/error rates and
differences from NanoFaaS are measured afterwards.

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

The 5% boundary is inclusive. The numerical comparison allows eight ULPs at
unity (approximately 1.8e-15 in the dimensionless cost ratio) for floating-point
rounding, including rates computed from completion counts and window duration.
This is not an allowance for measurement variability; observations above the
boundary by more than rounding still fail, and short windows require separate
assessment of sample counts and repeatability.

Every capacity/observer SDK trial persists its UTC measurement anchor and
warmup duration in its `manifest.json` before generator acquisition
(`physical-window/manifest.json` for observer variants),
including telemetry-off references and subsequent generator failures. Use that
anchor to select measurement requests; the earliest randomly scheduled request
is not the window boundary. Raw schedule phase labels alone do not separate
the technical SDK warmup from measurement.

The internal capacity-trial API accepts positive integer warmup and measurement
durations, retaining 30/60-second defaults for calibration callers. Longer
observer windows use the same durations for scheduling, generator drain bounds,
physical-proof selection, telemetry coverage and trial statistics. These
technical windows do not replace the nine scientific 360-second intervals.

The multifunction 360-second qualification preset declares a 2-second auction
budget and a 5-second preparation lead. The former 0.6-second budget censored a
first auction on fresh VMs; the new values are a candidate to qualify, not a
success verdict for those failed observations. Keep the first auction in the
evidence, all failures, and the unchanged 5% period-fraction/zero-censorship
timing gates. Solver, peer and preparation budgets remain 0.15/0.15/1 seconds.

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

Clock monitoring measures all six VM probes in scientific calibration, qualification and experiments. Legacy protocol v1 publishes edge clock health and enforces its UTC offset/uncertainty threshold, including cloud diagnostics. Protocol v2 keeps clock observations as diagnostic/correlation evidence: skew or a missing probe does not gate negotiation or request admission. Physical readiness, memory limits, peer identities and evidence-storage failures still fail preflight or collection as appropriate.

The run-owned HTTP clock probe disables Nagle through the standard-library
handler's `disable_nagle_algorithm` setting. Its separate header/body writes
otherwise introduce delayed-ACK latency on persistent connections, inflating
the RTT-derived uncertainty even on loopback. A rejected uncertainty bound is
not evidence of excessive clock skew: retain the measured offset and uncertainty
separately in failed runs. This transport correction does not relax the clock
threshold for v1 or retry an auction. Rebuild and freeze the packaged probe before
acquiring new data.

The existing multi-function Multipass presets use CPU quotas on a multiple of
0.01 CPU: 0.10 on edge-1/2, 0.14 on edge-3/4, 0.16 on edge-5 and 0.21 on cloud.
A declared 0.149 CPU was observed as `15000 100000` in `cpu.max` during the v4
bootstrap. These are existing preset values, not quotas recalculated when the
memory pool changes. Strict cgroup validation still compares Docker declarations
and kernel limits per container. Measured profiles retain their original
configuration scope; failed preflight evidence is retained.

Oracle rates use decimal quantum multiplication before JSON serialization. The runtime requires an exact decimal grid, so float multiplication tails such as `19 * 0.05 == 0.9500000000000001` are invalid. Oracle export rejects those off-grid values rather than treating them as equal within a tolerance; generated rates serialize as `0.95`. Parallel epoch preparation retains HTTP refusal status and response body in raw timing observations, marks the sample censored and never retries the trigger.

The strict five-function recipe reserves eight P2P transport slots: four for
endpoint advertisements at the central node and four for concurrent auction
requests. Both activities share the same bounded transport semaphore. The
default four-slot budget can reject auction traffic when advertisements overlap.
This resource setting applies equally to observer variants and scientific modes;
auction deadlines and acceptance thresholds are unchanged. Rebuild the runtime
distribution and freeze its updated recipe hash before acquiring new data.

Physical evidence collection retries a proof GET once after a transport or OS
read failure, including the final collection pass before teardown. Both failed
reads remain in `collection-errors.jsonl`, with their read-attempt number.
Invocation requests are never retried by this mechanism. HTTP refusals and
invalid proof identities are not retried; a persistently unavailable proof
remains censored. Rebuild the package and freeze its fingerprint before reuse.

## Clock probe diagnostics

The owned clock monitor preserves the selected offset/uncertainty sample in
`clock.jsonl` and every individual HTTP attempt in `clock-probes.jsonl`.
Raw attempts identify the node, zero-based attempt number within that measurement,
local before/after UTC timestamps in epoch seconds, remote UTC timestamp, RTT
and exception class. Attempts also carry host monotonic before/after brackets,
guest monotonic seconds and guest boot identity. RTT uses the host monotonic
clock; any host UTC step widens UTC uncertainty. A failed attempt remains on
disk. V1 fails immediately; v2 retains all attempts, selects a successful sample
if available, otherwise writes a `MISSING` diagnostic with null measurements.
Failure to write raw evidence still invalidates collection.
A custom selected-output basename produces the corresponding `-probes.jsonl`
companion. These records retain measurement uncertainty; they do not establish
that the entire RTT is actual clock skew.

### Logical protocol v2

The five-function live presets declare `oneShot.protocolVersion: 2`; omitted
versions remain v1 for existing scenarios and archived artifacts. V2 requires
NanoFaaS logical session APIs. NanoLab configures every edge with one shared
session and UTC grid labels, negotiates the configured number of rounds in
parallel, then explicitly activates the allocation at a host monotonic boundary.
Fixed rounds are not reported as convergence. Each trigger is sent once, with
raw negotiation/activation responses retained even if a later step fails.
Timing qualification excludes the intentional wait between negotiation and
activation; it includes both operational phases. Matrix cells and the final
artifact record the protocol version. A v1 qualification cannot qualify v2.

The manifest freezes the session, host boot identity and monotonic grid together
with UTC labels. A reboot cannot reuse that grid. Preparation, activation and
the load-task deadline use monotonic time. The existing k6 request schedule is
still an external UTC workload contract: a host clock step can spoil pacing and
invalidate the scientific data; this is distinct from v2 algorithm correctness.

Whole-VM raw samples retain guest UTC, guest monotonic time and boot identity.
After stopping and draining the VM samplers, v2 collection takes a terminal
diagnostic probe before freezing coverage. Missing terminal probes leave the
affected samples explicitly uncorrelated. Qualification prefixes count malformed
probe records from the parent scope used for correlation. Capacity and observer
trials also freeze a v2 monotonic measurement anchor and the exact workflow
ancestor that owns their probes (`probeParentLevels`, default one for existing
parent-scoped manifests). Nested trials use those original probe files for both
correlation and malformed-record accounting; they never copy or repair raw
probes. Their terminal probe runs after sampler drainage, including failed
trials. Legacy v1 manifests retain their original UTC interpretation.
Reports correlate v2 samples using the preceding and following host/guest
monotonic probes. A bracket crossing a phase boundary produces explicit phase
and epoch candidates; missing brackets or a reboot produce `uncorrelated`
samples, never a UTC guess. This does not assume equal clock rates. Graphs use
the interpolated host timeline projected on the frozen UTC anchor and preserve
guest UTC in hover data. Coverage reports distinguish missing samples from
uncertain phase assignments. The updated probe, sampler, package and protocol
identity require a new fingerprint and fresh overhead/calibration/timing
qualification; older runs remain evidence of their original configuration.

The bundled release recipes also follow the current upstream catalogue:
`forecasting` and `p2p-discovery` participate in the guarded `all` selection, and
six one-shot Rust images join the default group (the generic workload plus the
five campaign functions). The release matrix is now 9 JVM, 12 native and 33
default images per architecture, 54 total. This updates future release inputs;
older 48-image release evidence retains its original source. The deterministic
handler-envelope scenario includes those six functions. NanoLab's metric
catalogue collects the three node/peer P2P gauges without a function selector;
forecasting publishes no separate Micrometer meters.

## Controlled Linux host CPU placement

`oneShot.hostCpuPlacement` optionally assigns disjoint physical CPU sets to
every owned Multipass VM, the operator process and owned Loki/Grafana containers.
Each node declares `cpus` and `maxFrequencyKHz`; the CPU count must match its VM
vCPU count. `operatorCpus` and `loggingCpus` reserve separate host cores. Boolean,
negative, duplicate, overlapping and incomplete assignments are rejected.
Selected cores must be online, expose their maximum frequency, and retain the
`performance` governor. Each VM's cores must match its declared frequency family.
The runner reads these facts and does not change the host's frequency policy.

For the measured twenty-core host, a concrete campaign allocation is:

```yaml
hostCpuPlacement:
  nodes:
    edge-1: {cpus: [0], maxFrequencyKHz: 2808000}
    edge-2: {cpus: [10], maxFrequencyKHz: 2808000}
    edge-3: {cpus: [11, 12], maxFrequencyKHz: 2808000}
    edge-4: {cpus: [13, 14], maxFrequencyKHz: 2808000}
    edge-5: {cpus: [1, 2, 3], maxFrequencyKHz: 2808000}
    cloud: {cpus: [5, 6, 7, 15, 16, 17, 18, 19], maxFrequencyKHz: 3900000}
  operatorCpus: [4]
  loggingCpus: [8, 9]
```

This map is host specific; verify core identities before using it elsewhere.
It preserves the seventeen VM vCPUs and existing function limits. The two
logging cores retain the existing combined 1.5-CPU quota, leaving 0.5 CPU of
host reserve. VM pinning requires noninteractive `sudo taskset`; the selector
requires exactly one QEMU process whose disk path has the exact newly owned VM
name as a component. Every QEMU thread's resulting mask is verified. Placement
failure compensates the newly created VM. The operator's initial thread masks
are restored on exit, including workers created during the run. Foreign VMs and
external logging endpoints are not pinned.

Preflight rechecks hardware, operator masks and all owned VM thread masks and
records the evidence in `host_cpu_placement` and each node's `host_cpu_binding`.
Owned logging masks are retained in `logging-host-placement.json`. The declared
policy participates in the resource-scope hash; changing it requires a new
software/operator freeze and fresh overhead, calibration and timing evidence.
Without this optional policy, legacy workflows retain their existing behavior.
CPU placement does not alter one-shot routing or its fixed-round semantics.


## Autonomous supervision of the frozen functional observation

After preparing the freeze, launch everything with one command in the NanoLab Python environment:

```sh
.venv/bin/python /path/to/freeze/source-used.py run \
  /path/to/freeze /path/to/new-run --archive /path/to/new-archive
```

The frozen runner provisions the six owned VMs through Sonata, waits for cloud-init, starts services and logging, checks replicas and cgroups, measures observer cost, starts VM/container sampling and k6, runs the warmup and all nine 360-second intervals, writes the report and releases its owned resources. Sonata events cover acquisition, execution and compensation; an internal health thread runs during blocking waits without agent polling. It uses the configured operator CPU assignment applied by the existing CPU-placement resource.

Progress uses the ordinary task console by default. Select `--display json` for the established UTC JSON lines, `--display tui` for the existing workflow dashboard, or `--display none` for supervised execution without terminal I/O. The run directory contains atomic `status.json`, append-only `supervision.jsonl`, and final `result.json` with state, first cause, exit code, cleanup anomalies and archive verification. Heartbeats distinguish normal waiting from a completed stage; they are a reporting cadence, not a deadline or performance gate. During load they include mode, function scope and epoch from the frozen schedule. Supervisor thread CPU time is recorded alongside precheck VM sampler and collector cost observations.

An unexpected generator exit before all scheduled requests finish, a nonzero exit, a collector/sampler process failure, or a missing required control-plane/log collector in a valid sampler inventory stops the procedure once. Function request failures and HTTP rejects are counted separately in `responses.json`; function scaling, missing samples and uncertain clocks do not introduce exclusion criteria. Missing VM/container samples remain visible in raw files and coverage reports. SIGINT/SIGTERM trigger owned compensation, with exit130/143; operational failure returns exit1. No repair, retry or automatic relaunch occurs.

Archive copy and SHA verification start after load writers and the supervisor thread have stopped and joined. Final control-file updates are copied and verified serially; failed runs retain raw evidence. Watching stdout or opening status.json is optional: supervision and cleanup work even when neither agent nor human reads them. Capture stdout using the normal terminal/log facility for remote sessions; the command does not install an external messaging notification service.


The observation runner derives `nanofaas.forecasting.max-age` from the frozen
load horizon: 720 seconds warmup + 90 seconds schedule setup + 9 × 360 seconds
measurement + the 180-second generator allowance = 4,230 seconds.
The value and its basis are captured in operator-protocol.json and passed
explicitly to the control-plane startup command. A trace uploaded once remains
usable throughout these nine windows. The recipe's 5-minute default expired
before the first preparation in Observation03; native expiry, generation and
contiguous-window checks remain intact. The observation-readiness changes require rebuilding the runtime image and
creating a new freeze; historical distributions remain unchanged.

After compensation the runner also renders a missing report from retained raw
files and saves responses.json when a run fails, preserving the original cause
if report generation itself fails. Request rejects and errors therefore remain
counted separately from the final operational exit code.


A shared-iterations generator can return exit0 before the nominal last boundary
when every scheduled original has emitted and returned. HTTP rejects/errors
count as returned work. This is normal completion; collectors and VM/container
telemetry remain active until the full ninth interval ends. Missing emitted or
returned rows after the frozen boundary remain experimental observations. The
status and heartbeat report setup, warmup, campaign epochs0–8 and drain from the
same frozen grid. A late health fault during thread joining still writes result
and archive; a first signal during archive copy is deferred until evidence is
preserved and retains exit130/143. Later signals do not replace the first cause.


### Incomplete confirmations in an observation

The package builder `build_one_shot_observation_plan` constructs the actual Sonata
workflow. Preparation/fingerprint verification happens offline; the run command
uses real resource dependencies, compensation and task execution. The CLI entry
point delegates composition to this builder. It does not implement another engine.

An activation with `DEGRADED` and a usable plan continues through all nine epochs.
`executionUsable`, `fullyPrepared` and `measurementComplete` are independent facts.
A missing metric remains missing. `FAILED`, an absent plan, invalid ownership,
unauthorized outbound assignments, impossible evidence writes or a required process
failure still stop the procedure. The qualified comparison workflow retains its
separate acceptance rules; this observation never claims qualification.

`epochs.jsonl` retains node-local READY/ACK diagnostics, including identities,
generations, negotiated/confirmed quantities and the locally observed reason.
An ACK response lost at the buyer does not prove non-delivery at the seller.
`assignment-observations.jsonl` records unused inbound reservations. Reported
assignment quantities are flow quanta; planned cloud load is requests/second;
actual returned requests and their denominators are in `responses.json`. Do not
sum buyer and seller observations as distinct assignments. Partial terminal rows
are counted without changing the raw files.

Operational limits live in `OperationalLimits` and are frozen in scenario,
operator protocol and manifest: auction60s, solver/peer5s, physical preparation10s,
lead90s, API180s, prepare barrier60s, cloud-init1800s, service/Compose300s,
collector/sampler startup120s, replica startup300s, bootstrap invocation/proof120s,
collection HTTP10s, generator allowance180s, worker join300s and process drain,
exit and reader join30s. Runtime event retention remains10,000; streaming export
uses at most1,000 pages of100 and saves completed pages before a later read error.
No clock-skew or performance exclusion gate is added. Missing initial sampler
samples are reported if its process remains alive. The precheck measures current
collection cost before warmup; it does not apply the separate5% qualification gate.

Eight rounds, VM/function CPU/RAM, strict pools, handler concurrency, request30s
timeout, VU512, request retry0, workload, warmup720s and9×360s remain unchanged.

The fingerprint verifies the full nonignored runtime source inventory (including
new untracked assets), frozen package/recipes, and Sonata/provider implementation
bytes and dependency versions. Drift is rejected before provisioning. Progress
and final outcome are automatic; reading status is optional. After writer shutdown,
the supervisor updates the report's terminal outcome and verifies its archived
copy along with all raw data.


### Diagnostic collection and preservation failures

Observational metric/event HTTP reads run through the existing background
collector, outside the epoch preparation/activation driver. Epoch rows retain
measured operational wall time; unavailable timer values are null and reference
`metrics.jsonl` and `runtime-events.jsonl` for posterior analysis. A slow diagnostic
endpoint therefore cannot consume the next epoch's lead time.

The collector checks cancellation before requests, retries, candidates and each
batch of 16 proof lookups. A best-effort pass reuses the configured 30-second
process drain allowance and records incomplete collection on expiry. Final
normal snapshots use the same allowance and omit originals already proven
RELEASED. After a procedure fault, analysis uses retained raw files instead of
starting another potentially slow network collection. Persistence failures
propagate as operational errors; reporting errors are notes on the original cause.

A failed sole-copy raw export produces a nonzero result and incomplete cleanup:
owned Loki services stop but their volumes remain with `logging/backend-recovery.json`;
an unexported collector volume and its containing owned VM remain, with the VM
stopped and `logging/<node>/recovery.json` identifying the volume. Other owned
resources still clean up through Sonata. Recovery is manual; no replay, retry,
repair, relaunch or later deletion is automated.

## Optional continuous-arrival observation

The observation can replay a saved nonuniform calendar while keeping the original
ORACLE mean-rate trace. Export `arrival-profile.json` offline from the approved
curves in `dfaas-documents`, then add it during preparation:

```bash
uv run --frozen --no-sync --package nanolab python scripts/one-shot-observe.py prepare \
  --tuning /path/to/load-tuning-02 \
  --observations /path/to/observations.json \
  --distribution /path/to/distribution.json \
  --source /path/to/verified-runtime-source \
  --arrival-profile /path/to/arrival-profile.json \
  --output /path/to/new-continuous-freeze
```

Preparation validates and copies the self-contained profile into the new freeze,
including its SHA-256. Execution validates it before infrastructure acquisition;
it does not need the profile's original path. Every existing request retains its
ID, node, function, payload and interval; only `scheduledAt` changes. The exported
warmup preserves its duration and per-node/function counts and joins measurement
without a rate jump. The existing k6 asset reads the resulting schedule directly.
Without the option, the observation retains evenly spaced arrivals.

Launch the prepared freeze with the normal single supervised command:

```bash
uv run --frozen --no-sync --package nanolab python /path/to/new-continuous-freeze/source-used.py run \
  /path/to/new-continuous-freeze /path/to/new-continuous-run \
  --archive /path/to/new-continuous-archive
```

The raw run/archive contains `arrival-profile.json`, `schedule.json`, the profile
hash in manifest generator metadata and the existing actual emission timestamps.
Compare emitted load and arrival lateness with the saved calendar; request errors
and rejects remain experimental outcomes. No extra runner worker, mathematical
dependency, resource quota or performance/clock exclusion criterion is added.
Historical freezes require no editing and cannot enable the feature by changing
an external profile after preparation.

`prepare --forecast ewma|holt|ari` replaces the ORACLE with one of NanoFaaS's load
forecasts, every observation reports the forecast error per interval, edge node and
function in `forecast-errors.csv`, and `scripts/one-shot-forecast-replay.py`
replays a finished run through the forecasters offline; see
[the one-minute observation](testing/one-shot-multipass/continuous-60s-inputs.md#the-same-minute-run-with-a-nanofaas-load-forecast).

## Operational failures, finalization and configuration

The first operational failure is the structured cause in terminal receipts.
Service acquisition preserves an initial readiness failure or interruption even
if closing the probe worker and releasing the service also fail. Both cleanup
operations are attempted once; later failures are exception notes. Sonata keeps
declared dependency order and compensates only acquired resources. Primary
SIGINT and SIGTERM outcomes retain exit codes 130 and 143.

The observation launcher attempts final VM inventory and offline reporting
independently. An inventory error is recorded in `inventory-failure.json` when
storage permits; an error writing that receipt does not skip the report attempt.
Later inventory, receipt and renderer failures become notes on the first cause.
The supervisor updates any produced report with the terminal operational outcome.
Request rejects and errors remain experimental observations.

After owned writers stop, the supervisor claims a new archive destination and
copies and verifies raw evidence with SHA-256. An existing archive destination
is never overwritten. A failed copy or final verification leaves
`archiveVerified=false` and invalidates terminal receipts and reports in raw and
owned archive locations wherever they are writable. This diagnostic finalization
never repairs corrupted measurement files, retries verification, or relaunches
the workflow. Corrupted archive evidence and intact raw measurements are retained.
If storage rejects failure receipts or report updates, consistency cannot be
promised for those files; available raw receipts and a healthy selected display
record the persistence or archive-invalidation limitation while preserving the first cause.

Scenario CLI artifact references, including the optional
`oneShot.observability.vmResourceOverhead`, resolve relative to the scenario file.
Absolute references remain supported. Missing files and incorrect SHA-256 values
still fail through the existing verified artifact reader. The observation
launcher's own bootstrap and telemetry precheck follow their existing path.

One-shot images are selected through `recipeProfile` and the runtime distribution.
Omitted image fields, `controlPlaneImage: null` and `functionImages: {}` are valid
defaults. A control-plane image value or nonempty function-image mapping fails
validation before acquisition with:

```text
one-shot does not support controlPlaneImage or functionImages overrides; select images through recipeProfile and the runtime distribution
```

Generic comparison/loadtest image overrides retain their supported behavior.
Future observations must prepare a new freeze with the fixed package and launcher.
The package fingerprint covers the supervisor, catalogue and scenario code;
execution also verifies the active launcher against the saved `source-used.py`
hash. Existing freezes, historical results and their drift checks remain intact.

### Follow-up: interruptions and partial report updates

The finalization review of `14f261e` found additional cases beyond the original
five fixes. An interruption received during terminal receipt or report writes
now causes a bounded update of the newly observed failed snapshot, whether or
not an archive is selected. The supervisor detects the change relative to the
snapshot being persisted, including when the interruption has already become
primary. Healthy verified archives are not followed by unconditional raw rewrites.

Terminal report updates attempt each existing valid JSON/HTML output independently.
An unreadable or unwritable output does not prevent updating the other valid
output. Missing reports remain allowed for early failures. The HTML's unique
escaped JSON summary is read and compared with the JSON scientific payload;
only differences in `operationalOutcome` may be reconciled. Different scientific
fields or missing, ambiguous or malformed HTML summaries remain errors. Chart
content and all bytes outside the selected summary block are preserved.

When a report write changes the operational outcome, a single diagnostic pass
updates writable outputs to that failed outcome. A partial report update during
archive finalization stops the successful completion path and invalidates the
owned archive with `archiveVerified=false`. Its report is finalized independently
of raw report writability. Corrupted measurements, existing unowned destinations,
cleanup ownership and normal hash verification keep their existing behavior.

If an earlier workflow failure or interruption remains primary, its cause/code
survive and secondary report-channel limitations are retained in the available
terminal receipts and a healthy selected display. A writable report reflects
failure even if the other output cannot be updated; no consistency is promised for unavailable or
scientifically invalid outputs. This is diagnostic finalization, with no retry
of the workflow, acquisition, measurement or failed archive verification.

## Observation presentation selection

These are invocation examples; choose one display when launching a normally
prepared observation (do not run them as verification commands):

```bash
uv run --frozen --all-packages --all-groups python scripts/one-shot-observe.py run FREEZE RUN --archive ARCHIVE --display console
uv run --frozen --all-packages --all-groups python scripts/one-shot-observe.py run FREEZE RUN --archive ARCHIVE --display tui
uv run --frozen --all-packages --all-groups python scripts/one-shot-observe.py run FREEZE RUN --archive ARCHIVE --display json
uv run --frozen --all-packages --all-groups python scripts/one-shot-observe.py run FREEZE RUN --archive ARCHIVE --display none
```

`console` is the default. Scripts consuming the former JSON stdout must select
`json`. `--display` and `--archive` can precede or follow the positional paths.
TUI selection is explicit, uses the existing dashboard and log pane, and never
waits for acknowledgment at campaign exit. Other interactive workflows retain
acknowledgment on terminal input, including after a workflow error. This adds no
new TUI menu, attach service or resume mechanism.

`none` constructs no UI and reads/writes no terminal input/output. Health checks,
recording, signal handling, report finalization and owned cleanup remain active.
Programmatic `CampaignSupervision` inherits the active Sonata sink when omitted;
`presentation=None` explicitly disables inheritance. A caller-provided sink is
borrowed, receives original task events once, and is restored on every exit.
Its outer lifecycle remains the caller's responsibility. The observation
launcher owns its selected view through the supervisor's factory.

A selected display failure is operational: acquisition/load stops, raw evidence
is retained and Sonata releases only acquired resources in dependency order.
Subsequent rendering to the failed view is disabled. No replacement display is
opened. Creation, refresh, input listener and disposal failures participate in
first-cause handling; earlier failures and signal codes 130/143 survive.
Background recording or rendering failures wake active waits without status
polling. Request rejects and errors remain measurements.

`RUN/supervision.jsonl` and `RUN/progress.jsonl` retain complete structured rows;
`RUN/status.json` is replaced atomically and `RUN/result.json` records the terminal
outcome. Writable JSON/HTML reports and owned archive receipts are finalized to
that outcome. Storage errors may prevent individual diagnostic channels from
being updated; no consistency is promised for unwritable files.

The live TUI remains available for cleanup and initial archive progress. Its
listener is joined and display disposed before the final control-file copy and
SHA verification. Any close failure is persisted before that verification; late
outcome changes invalidate owned success receipts through the bounded diagnostic
path, without recopying measurements or retrying a failed hash check. Consult the
terminal artifacts for the authoritative result, particularly after the TUI has
closed. Console/JSON sinks have no background lifecycle and emit the final
post-verification summary.

Future normal preparations fingerprint the new NanoLab modules, active launcher,
Rich, TUI toolkit and Typer sources and versions. Existing freezes and historical
fingerprints are unchanged; code drift still stops before inventory/acquisition.
See [verification evidence](testing/one-shot-reporting-separation.md).
