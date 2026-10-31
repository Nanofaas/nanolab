# B6 — Idle footprint versus request reactivation latency

Status: dedicated NanoLab workflow implemented and locally verified on 2026-10-31;
cloud pilot not started.

## Purpose and paper claim

B6 asks: under a fixed deployment budget, how much memory is retained when a
function is kept ready, and what service penalty accompanies releasing its
replica between sparse requests? It complements B5's active-runtime measurements
and B1.1's concurrency measurements. A useful result includes a small memory
benefit, failed wake-ups or a large first-request penalty; a positive result is
not an acceptance condition.

This is a bounded lifecycle trade-off supporting the paper's resource-tailoring
argument. It does not establish replica-controller quality, concurrency scaling,
the cost of excluding a module, or the startup performance of the control plane.
The manuscript still contains an RQ3 about concurrency/replica interaction;
B6 does not answer that question. Reconcile that wording separately when the
campaign scope is incorporated into the manuscript.

## Approaches and proposed choice

1. A functional zero-to-one check proves the capability but cannot quantify its
   resource/service trade-off. Keep it as a qualification gate.
2. **Recommended: paired keep-ready and scale-to-zero runs**, with the same
   post-burst idle duration, function and image identities. Measure memory throughout
   the cycle and client-observed correct-response latency. This adds a resource
   comparison to the previous cold-start proposal.
3. A matrix of control-plane builds, runtimes, image downloads and autoscalers
   mixes several questions and multiplies cost. Exclude it from B6.

The author authorized this paper-oriented B6 and specified a new workflow that
reuses NanoLab components and conventions. Final campaign size remains provisional.

## Fixed system and treatments

- Five equivalent B5 word-statistics functions: Java, Go, JavaScript, Python,
  Rust. One function is deployed at a time. Retain the generated output oracle
  and the four medium inputs; rotate by global cycle index, identically in both
  treatments. Compare complete runtime stacks, not language alone.
- Kubernetes on workflow-created Azure Spot VMs. Reuse the campaign's D8s_v5
  stack/load-generator allocation and record the actual VM/CPU identity.
- Control plane stays running: JVM full tiering, Serial GC, 2 CPU / 2048 MiB
  limit, with the same module-containing image in both treatments. Requests:
  0.25 CPU / 512 MiB. No native-control-plane comparison in this experiment.
- Function limit 2 CPU / 512 MiB, request 1 CPU / 256 MiB. Same image digest,
  resource settings, queue configuration and fixed concurrency in both arms.
- Keep B5's async-queue path for synchronous calls: concurrency 2, queue 20,
  execution timeout 30 s, maximum server retries 3, client retries 0. Freeze and
  archive the exact effective semantics; a retry is evidence, not a new client
  sample. Client timeout 40 s in both arms; do not enlarge it after observing
  failures. The existing wake-up timeout is 30 s and is recorded separately.
- Both treatments retain the INTERNAL scaler, 5-second polling, the existing
  30-second scale-up / 60-second scale-down cooldowns and the existing loadtest
  RPS target of 100. These are operational settings, not an idle timeout. The only
  treatment difference is the replica floor: keep-ready min=max=1; scale-to-zero
  min=0, max=1. The maximum of one excludes horizontal capacity scaling.
- Pre-pull function images through existing deployment/build steps, qualify the
  handler, and verify image identities/cache presence before measurement. No
  image-pruning during a run. This measures pod/process reactivation with cached
  images, not first-time image download.

## Pilot calendar and lifecycle evidence

The pilot uses three measured cycles per treatment and runtime, with a common
180-second idle interval. It is a measurement-validation pilot, excluded from
the final statistical campaign. The pilot must establish whether this interval
permits natural parking and at least 60 seconds of zero-state observation; do
not shorten internal cooldowns to force a result.

Each arm includes one final 180-second idle interval to observe the return to
zero after its last burst. Across five runtimes and two treatments, pilot idle
time is therefore 120 minutes, plus about 10 minutes of warmup. Provisioning,
builds and requests are additional. The provisional final repetitions below
require 16.5 hours of idle time plus about 30 minutes of warmup and overheads.
These are cumulative times, not parallel wall-clock estimates. Final sample
size/duration is decided after the pilot, not silently expanded.

The controlled exposure is a 180-second idle interval after the preceding burst
has fully drained, not an open-loop calendar with identical absolute arrival
times. Cold response time may lengthen a cycle. Record that difference; compare
equal idle windows and report actual whole-cycle durations alongside integrals.
Do not derive equal-calendar throughput or memory savings from these runs.

Before each arm, deploy a fresh control-plane instance using the same image,
qualify all four inputs, and run the same bounded warmup (60 requests, spaced at
least one second apart).
Warmup requests and their results remain in the ledger but outside the measured
cohort. The scale-to-zero treatment then receives no invocations during idle.
Do not manually scale/delete pods to manufacture a cold start.

For each cycle, after the preceding request burst has completed:

1. Observe 180 seconds without function invocations, retaining resource samples
   and lifecycle observations throughout. Management observation must not invoke
   the function. Use the same post-burst idle duration in the paired keep-ready arm.
2. Record the final 60-second idle window. In the zero arm require desired and
   ready replicas zero and no remaining function pod, including terminating
   pods. Missing API data is unknown, never zero. In the keep-ready arm require
   one ready pod with no restart or replacement during this window.
3. Send one synchronous request with the cycle's input and a unique request
   identity. Record monotonic client start/end, status, transport failure and
   exact output validation. Latency ends when the full response body arrives;
   exact output validation determines success outside the timed interval.
4. Following a correct response, immediately issue five sequential requests with
   the same input, recording them as post-reactivation observations. Their pod
   must remain the same; these are early subsequent requests, not evidence of
   steady-state JIT performance. If the first request fails, record followups as
   not attempted rather than treating recovery requests as warm observations.
5. Observe the return to zero, including old/new Pod UIDs, container restart
   counts, creation/readiness observations and final deletion. The next idle
   interval starts after this cycle's requests and server-side work complete, not
   after zero is detected. Thus slow parking remains visible in the fixed idle
   budget. In the ready arm also verify that the replica remained ready.

Requests still occur if a lifecycle precondition fails, but the cycle is marked
as a lifecycle failure and cannot enter the qualified cold-latency distribution.
For each configured cycle record one outcome, including not attempted after an
aborted arm. Report service success (correct first response) separately from
qualified wake-up success (verified zero, new Pod UID, correct first response
with no unexpected restart/replacement). Show both offered and configured
denominators, plus explicit counts for not-attempted and unresolved cycles.
Do not count a correct warm response after a failed parking check as a wake-up.
Do not wait indefinitely,
silently extend idle windows, drop failures or resample a failed cycle.

A client timeout does not establish server completion. The current interface
cannot prove that all queued work, retries and wake-ups have drained after a
failed response. Therefore stop that arm immediately after any invalid response,
timeout or transport failure; preserve its remaining cycles as not attempted.
An interrupted command with an unmatched started event stays unresolved.
The same rule applies to both arms; no automatic rerun erases a failure.

Primary timing is on the load generator's monotonic clock. Kubernetes events
and sampled readiness provide explanatory timings with their observation
resolution declared; do not subtract cross-host wall clocks to claim exact
scheduling or initialization subcomponents. Preserve SDK cold/warm signals
as diagnostics. Do not compare `init_duration` across SDKs: their boundaries
currently differ, and some include handler execution.

## Resource boundary

Report working set and RSS separately for the control plane and function,
retaining container/pod identities and raw samples. Never add overlapping parent
and child cgroups, or RSS to working set. Prometheus range queries have a 5-second step over cAdvisor's 10-second
scrapes; repeated range points are not independent observations. This supports
idle means, not startup peaks. Integrate samples from within the requested window
only, holding the nearest boundary value for at most 5 seconds and rejecting gaps
above 10 seconds. Missing coverage stays unknown, including whole-cycle coverage
while a newly created container has not yet appeared in cAdvisor.

Report the paired idle-window footprint and the integral of memory over the
whole observed idle-and-request cycle (MiB seconds), with its duration. Include
the time spent waiting for parking; do not extrapolate zero-state savings over
the entire idle interval. Preserve signed differences and missing coverage.

The primary bounded accounting is control plane plus function containers.
Record supporting Kubernetes/runtime/monitoring resource costs separately where
observable, explicitly naming uncharged host services. A component-memory drop
must not be described as reclaimed whole-node RAM or a complete-deployment
percentage. Missing function metrics become zero only with verified pod absence
and a healthy metrics pipeline; stale terminated-pod samples are not live memory.
Do not fill missing control-plane data with zero.

## Repetitions, analysis and paper outputs

The proposed final campaign uses three independent VM-pair allocations per
runtime and ten cycles per treatment within each allocation (30 cold attempts
per runtime). Balance treatment order across blocks and shuffle runtime order
with a recorded seed. Use a fresh control-plane deployment for each arm and
retain the same images and telemetry. Cycles share a host and are not independent
host replications. Freeze final idle duration, repetitions and total cost/time
estimate after pilot inspection and before the final run; pilot data are excluded.

Use Python scripts to reconstruct and independently confirm every table from
the archived raw ledgers and resource series. Validate cycle counts, input
rotation, lifecycle predicates, output correctness, timeout/error counts,
memory coverage and paired treatment identities before producing claims.

The paper receives one table per runtime with idle footprint in both states,
first-response and early subsequent-response latency, and successful wake-ups
over all attempts; one figure plots idle memory difference against first-request
latency penalty. Show individual observations and per-block summaries. With
three independent blocks, avoid strong population-level uncertainty claims;
30 cycles do not support a robust p99. Do not pool repeated followups with first
requests or omit unsuccessful attempts from outcome reporting. No estimate of
energy savings, monetary savings or a latency/memory break-even without a stated
application objective and additional evidence.

## Reuse and implementation scope

Inspection baseline: NanoLab `0c175528c49bcf7b99ddc653e98519f1ebfd5eb4`,
NanoFaaS `8715e8e7f6727a258cf43088b9113ac10767d69b`.

Add the dedicated `idle-reactivation` workflow, its strict `idleReactivation`
configuration block and five bundled runtime presets. Reuse `add_platform`,
function resolution, Helm settings, role-bound executors, provisioning, the B5
output oracle, Prometheus capture and file fetching. Build images once, release
the first arm, deploy the second from the same images, release it, then run the
paired evidence gate. Keep the existing loadtest plan unchanged.

The new code owns the sparse request protocol, lifecycle qualification and
paired report. The load generator runs a bundled stdlib Python probe. Preserve
request-start and response events, response bodies, pod identities, raw Kubernetes
observations, labelled metrics, protocol and oracle inputs for offline checks.
Require distinct stack/loadgen machines and a fresh, complete run; reject resume
and partial-task selection. Missing observations, terminating pods, changed images,
invalid outputs and partial ledgers cannot qualify success. No NanoFaaS change
is required by this implementation.

Launch only through NanoLab from `michele@4.245.12.45`, in a new tab of
`unique-crab`, with fresh isolated NanoLab/NanoFaaS checkouts, temporary files and
results under `/data`. Preserve existing experiments. Build steps may use all
available CPUs and practical available memory; measurement limits remain fixed.
Commit the resolved manifest and source/image identities before measurement.
Archive compressed raw data plus member hashes in nanofaas-paper before analysis.
Spot interruption yields an archived incomplete block; repeat the entire paired
block under a new identity, never splice it into the interrupted block.

## Implementation and pilot boundary

The author approved implementation, amending the architecture to a dedicated
workflow. The implementation plan is in NanoLab under
`docs/superpowers/plans/2026-10-31-idle-reactivation.md`. Validate the workflow
locally and review it before the bounded cloud pilot. Freeze the final campaign
only after the pilot establishes lifecycle and metric coverage. No B6 cloud
resources have been acquired yet.
