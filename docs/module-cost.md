# Matched module cost

This workflow asks how much retaining inactive adaptation modules costs relative
to excluding them at build time. FIXED concurrency and NONE scaling disable
adaptation, not initialization or periodic background work. It is not a load
capacity or autoscaling experiment.

Prepare three independent blocks on the controller (with NanoLab installed):

```sh
python scripts/module-cost-prepare.py --ssh-key /home/michele/.ssh/id_ed25519.pub
```

The printed directory is under `/data/module-cost` and contains validated
scenarios, Azure Spot environments, recipe previews, ordered commands and a
manifest. Review and commit these input files before execution. Each command
uses ordinary `nanolab run`; the workflow owns stack and loadgen provisioning
and cleanup. Use unique block names and a fresh result directory. Interrupted
blocks are retained, not resumed or combined with replacement VMs.

Eight arms combine JVM/native with Base, concurrency-control, autoscaler, or
both modules. Base includes k8s-deployment-provider, async-queue and
build-metadata. Native uses O3 and Serial GC; both modes have a 64 MiB maximum
heap. Every registration uses the same function image digest, concurrency 2,
queue 20, no retries, NONE scaling and one replica. Control plane limits are
2 CPUs/2048 MiB and function limits are 2 CPUs/512 MiB.

All eight control-plane builds and the single Java function build precede any
measurement. Gradle workers/native parallelism use the VM's CPUs; native build
memory uses the host's physical memory, and Gradle's inherited 1 GiB heap is
replaced by MaxRAMPercentage=100. These are upper bounds, not reservations;
builds and the OS still share memory. Actual commands, capacity, recipe hashes,
source inventories, plugin reports, artifact file sizes and image identities
are archived. Image size means uncompressed Docker image bytes; application
files are listed separately, including configuration and native libraries.

Each fresh deployment uses cached containerd images, qualifies the four exact
word-stats outputs, warms for 60 seconds at 40 RPS, drains, remains idle for
180 seconds and receives 180 seconds at 40 RPS. k6 records every send and full
terminal response, including failures; no retries are added. A boundary arrival
may add one request, explicitly retained in the observed scheduled cohort.
The primary resource window is the final 120 seconds of idle. Working set and
RSS are separate time-weighted signals, with CPU derived from raw counter
changes. Function resources are reported separately. Missing coverage or
changed pod/host/resource identity invalidates the paired comparison.

Readiness is **container running to Pod Ready**, measured from Kubernetes
status timestamps on the stack clock (one-second resolution). It excludes
scheduling and image pulls, but is not an instrumented process-start stopwatch.
Startup logs must corroborate each included adaptation module, including in
native images. An absent series is never proof of module elimination.

Offline regeneration requires only the retained block and NanoLab:

```sh
python -m nanolab.tasks.module_cost_report /data/module-cost/CAMPAIGN/BLOCK/results
```

`analysis.json` retains incomplete arms and reasons; no contrasts are generated
for an incomplete block. Complete blocks produce C−Base, S−Base, CS−Base and
CS−C−S+Base effects separately for each runtime. The three VM blocks, not the
requests, are the replication units. Paper scripts live in
`nanofaas-paper/experiments/module-cost/analysis/` and independently confirm the
primary integrals and counts before rendering from compressed archives.

No physical campaign has qualified this workflow yet. The first run must check
native module retention, resolved policies, the offered load and metric
coverage; a failed qualification is evidence, not a zero-cost result.
