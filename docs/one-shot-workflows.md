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
native prepare concurrently on both edges, without load injection, and retains
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
