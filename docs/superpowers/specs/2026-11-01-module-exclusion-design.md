# Module exclusion versus inactive adaptation: workflow design

Status: proposed implementation design, following the author's agreement on the
comparison and measurements. No workflow implementation or infrastructure launch
is part of this document. Numerical settings below are design defaults, not
claims about measurements or additional author decisions.

## Paper role and research question

The author clarified that fixed-target offload and one-shot demonstrate operation
of the proposal in an edge-cloud ecosystem. Their primary evidence is successful
end-to-end operation and service behaviour across the tested edge/cloud topology.
Architectural reuse is explanatory context, rather than their primary research
question. Pending runs must not be described as completed evidence.

This separate experiment addresses RQ1b: what resource cost is avoided by omitting
unused adaptation modules from a distribution, compared with retaining them while
holding function concurrency and replica count fixed? Primary outcomes are idle
control-plane memory and CPU. Artifact size, readiness and loaded behaviour are
secondary outcomes. Zero or negative savings remain valid observations.

## Source inspection and the meaning of inactive

Inspected NanoFaaS b1cb1608f414df87fb039783aa8ecd6715b7db8e and NanoLab
85b7c479d7b665e9e24c9344ce2ad64bf31510d9. Freeze the actual implementation
revisions separately before execution; this design is not a source pin for a run.

`ConcurrencyControlConfiguration` and `AutoscalerConfiguration` are conditional
on supporting beans. They have no inspected global `enabled=false` switch.
`ConcurrencyGovernor.start()` starts a periodic scheduler; in `FIXED` mode the
coordinator reapplies the declared concurrency. `InternalScaler.start()` also
starts its scheduler when managed replica control is available, while its loop
only scales functions whose strategy is `INTERNAL`.

Therefore the retained-module treatment uses concurrency `FIXED` and scaling
`NONE`. It disables adaptation, not bean construction or all background work.
The manuscript must use this precise description. Do not add a new disable flag
merely to obtain a favourable comparison. Do not use `minReplicas=maxReplicas`
with active INTERNAL scaling as a substitute for NONE.

## Approaches

1. **Chosen working design:** exclude modules versus retain them with inactive
   adaptation, using existing runtime settings. This directly measures the cost
   of shipping unused capabilities without changing NanoFaaS semantics.
2. Exclude Spring auto-configuration as well as disabling adaptation. This asks
   a different question about initialization and may eliminate beans during AOT;
   it is outside the initial workflow.
3. Compare excluded modules with active controllers. B1.1/B6 already address
   controller behaviour; this would confound capability cost with adaptation.

## Matrix and controls

The fixed base comprises `k8s-deployment-provider`, `async-queue` and
`build-metadata`. Keep queue/provider/telemetry identical in every arm.

| Composition | Additional modules | Runtime function policy |
|---|---|---|
| Base | none | FIXED, NONE; one replica |
| C | concurrency-control | FIXED, NONE; one replica |
| S | autoscaler | FIXED, NONE; one replica |
| CS | concurrency-control, autoscaler | FIXED, NONE; one replica |

Use the exact same explicit function registration in all arms, including FIXED
and NONE even when their corresponding modules are absent. Never rely on an
omitted scaling strategy: `FunctionSpecResolver` defaults it to INTERNAL.
Archive and compare the resolved registration, not just the submitted document.

Use JVM with JIT and native: eight distinct configurations. JVM/native contrasts
are secondary; module contrasts are computed within each compilation mode.
No extra GC, heap, language or resource-budget axes are introduced.

Proposed defaults: one Java word-stats function with the archived medium corpus
and exact runtime oracle, concurrency 2, queue capacity 20, one replica, no
application or client retries. Control-plane limits 2 CPUs/2048 MiB, function
limits 2 CPUs/512 MiB. Reuse Serial-GC recipe settings; freeze an explicit 64 MiB
maximum control-plane heap in both modes, with no heap below 64 MiB. Pin effective
native optimization and JVM options from the actual build report, not a recipe's
filename. Build processes receive no added CPU/RAM limits.

These are new within-experiment matched contrasts, not a promise of numerical
comparability with historical runs using different heaps or software revisions.
The same function image digest is reused across all eight arms in a block.

## Placement and run lifecycle

Propose two Azure Spot VMs per repetition block: stack/build VM and separate
load generator, provisioned and cleaned up by NanoLab/Sonata. Use existing Azure
presets (initial stack size Standard_D8s_v5 with 128 GiB disk); freeze generator
size, image and region in the manifest. Controller output and temporary campaign
storage belong under `/data`. This leaves DGX availability for edge-cloud work.

Use three independently provisioned blocks, each containing all eight arms:
24 measured deployments. Schedule one arm at a time; start a new control-plane
process and new function pod per arm. Record a seeded order and counterbalance
orders across blocks as far as three blocks allow. Publish the actual order.
The VM block, not individual requests or scrape samples, is the replication unit.

Validate recipes and archive sources before provisioning. Build the eight
control-plane artifacts and one function artifact before measurement, using the
existing recipe pipeline. Build provenance includes cache state; do not report
cached or differently warmed builds as comparable cold-build observations.
Deploy each arm through `add_platform`, qualify it, warm it, measure, collect
before teardown and record an immutable attempt receipt. Retries create new
attempts. An interrupted or evicted block stays identifiable; do not silently
pair observations from different hosts or sessions.

## Measurements

- **Artifact:** control-plane JAR or executable bytes and image metadata. Report
  compressed/uncompressed sizes distinctly; shared image layers are not additive.
- **Readiness:** monotonic time from process/container launch to successful
  control-plane readiness, with images already local. Record first correct
  function response separately; exclude VM provisioning and image download.
- **Warm idle:** after identical qualification and 60 s warmup, observe 180 s
  without invocations, after all warmup requests have drained. Primary memory is the time-weighted working set over the
  final 120 s; retain RSS separately. CPU is cumulative control-plane CPU seconds
  divided by the same window. Do not add RSS and working set.
- **Loaded:** propose 180 s at 40 requests/s, reusing the existing runtime
  campaign's low-rate setting, followed by a bounded 40 s drain. This is a fixed
  work comparison, not a saturation search. Preserve every send and completion,
  input identity, output validation, status and full-response elapsed time.
  Retain actual scheduled, emitted, completed, refused, errored and missing work.
  Phase assignment uses request start time; completion during drain remains in
  that request's cohort.
- **Resources under load:** separate control-plane and function cgroup memory
  and CPU, and report memory coverage. Keep the generator outside the measured
  control-plane boundary; retain its delivery diagnostics.
- **Mechanism:** archive declared/effective module sets, recipe hashes, runtime
  policies, resolved function registration, desired/ready replicas, effective
  concurrency, pod/image identities and restart counters. Capture initialization
  diagnostics outside primary sampling windows. Module absence must be established
  from the distribution, not inferred from a missing Prometheus series.

Readiness timing and diagnostic collection must not force GC or alter one arm's
warmup. Build wall time may be retained as provenance. Build peak RAM/CPU is not a
primary result unless all actual workers (including Docker/BuildKit/compiler)
are inside a verified accounting boundary; the client process's RSS is insufficient.

## Validity and interpretation

Confirm one ready replica and the declared effective concurrency throughout.
An adaptive decision, restart, changing function digest, unexpected module set,
wrong body, missing required evidence or mismatched resource limit invalidates
the intended contrast while preserving raw results. Errors/refusals/drops remain
measured outcomes, not reasons to delete an attempt. If work delivered or completed
differs materially, report it and withhold an equal-work resource-saving claim.

Native images may remove unreachable implementation details. Do not assume that
selected module metadata proves retained runtime code. Verify the expected
initialization/component evidence in the included treatment and report eliminated
or unavailable cases explicitly. Because FIXED/NONE are function-registration
settings, do not silently reinterpret them as build-time AOT disabling.

For each runtime and block compute C-Base, S-Base and CS-Base; also report
CS-C-S+Base as a dependency/initialization interaction contrast. For memory/CPU,
a positive included-minus-excluded difference denotes a saving from exclusion.
Display all block contrasts and their spread. Three blocks do not justify precise
tail or population claims; do not bootstrap individual requests as independent
experimental repetitions. No advantage is assumed in advance.

## Reuse and minimal additions

Add a dedicated `module-cost` scenario, following `idle-reactivation`'s composition
of shared resources. Reuse `ScenarioConfig`, ordinary `nanolab run`, provisioning
and role executors, `PlatformRequest`/`add_platform`, recipe distribution verification,
`RunK6Task`, remote fetching, Prometheus snapshots, pod inventory parsing and
existing medium-input/oracle helpers. Reuse `comparison.cells` identity/attempt
primitives where their contract fits; do not force the experiment-01 QR matrix or
its sustainability thresholds into this workflow.

Use one small preparer for manifests, block order and normal workflow commands.
A block invocation measures its eight configurations; the preparer supplies the
three unique block environments. No second cloud launcher, generic campaign
framework, new analysis dependency or duplicate runtime workload is needed.
All analysis, numerical tables and figures are generated and independently
checked by Python from preserved evidence. Execution and publication remain
separate steps from this design.

Implementation tasks: [plan](../plans/2026-11-01-module-exclusion.md).

Design review: an independent source review identified the implicit INTERNAL
strategy default; this document now requires explicit FIXED/NONE in every arm.
No other important unresolved design questions were reported. Runtime qualification
and effective native-component retention have not yet been tested.
