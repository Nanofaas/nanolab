# B6: idle footprint and reactivation

`idle-reactivation` measures the memory retained by a ready function against the
first-response cost after naturally reaching zero replicas. It is a dedicated
workflow, using the same platform resources, role executors, provisioning and
artifact collection as other NanoLab workflows. No NanoFaaS changes are required.

Five bundled scenarios select the equivalent B5 wordstats implementations:
`idle-reactivation-java.yaml`, `-go.yaml`, `-javascript.yaml`, `-python.yaml`, and
`-rust.yaml`. Each run measures one runtime with two fresh control-plane deployments.
Images are built once and reused. The workflow releases each deployment after
measurement, before the final paired evidence gate. VM cleanup remains owned by
the standard environment provisioner, including on failure.

## Run through the product CLI

From a fresh checkout on the campaign controller, use a managed Azure environment
with distinct `stack` and `loadgen` role names, `priority: Spot`, no preconfigured
hosts, and the campaign's D8s_v5 allocation. Create an environment file from the
bundled `azure-comparison-spot.yaml.example`, set both VM sizes and unique names,
and fill in the resource group and SSH key. Keep source checkouts, manifests and
results under `/data`. Existing runs must have different names and directories.

```sh
nanolab plan idle-reactivation-java.yaml --environment /data/b6/environment.yaml \
  --run-dir /data/results/b6-java-block01
nanolab run idle-reactivation-java.yaml --environment /data/b6/environment.yaml \
  --run-dir /data/results/b6-java-block01
```

Use NanoLab's normal workspace source configuration to point at the fresh
NanoFaaS checkout. Previewing a plan does not acquire machines. `run` owns
provisioning and cleanup; do not precreate machines or invoke the probe by hand.
Resume, task selection and keeping a deployment are rejected for this paired
protocol. Each attempt needs a new output directory. A Spot interruption remains
an incomplete block; repeat both treatments with a new identity, never splice runs.

## Fixed protocol

The `idleReactivation` block contains only `cycles` (default 3), `idleSeconds`
(default 180, minimum 120) and `zeroFirst` (default false). Alternate treatment
order across independent blocks. Other loadtest options are rejected.

The control plane retains full JVM tiering, Serial GC, the autoscaler and
async-queue. Its budget is 2 CPU/2048 MiB, requested 0.25 CPU/512 MiB. The function
has 2 CPU/512 MiB, requested 1 CPU/256 MiB, concurrency 2, queue 20, execution
timeout 30 seconds and three server retries. Client retries are disabled and the
full-response deadline is 40 seconds. The workflow adds no build quotas. For the cloud campaign, apply and record the
existing NanoFaaS build-resources patch (85% Java/Node build heap, available CPUs)
in the fresh source checkout before freezing provenance; otherwise the source's
Gradle heap default still applies. Runtime budgets remain fixed.

The INTERNAL scaler uses min/max 1/1 in `keep-ready` and 0/1 in `scale-to-zero`,
with the normal poll/cooldown settings and RPS target 100. Both use cached images
(`IfNotPresent`). Qualification invokes all four medium inputs against the B5
oracle; warmup sends 60 requests at least one second apart. A cycle waits the
configured idle duration, qualifies its last 60 seconds, and sends one request
followed by five immediate sequential requests with the same rotated input.
A final idle interval verifies return to the expected idle state. Any failed
response stops that arm because server completion can no longer be established.

The last idle window must show the expected replica count and pod identities;
zero means no function pod, including terminating pods. A successful cold call
must create a new pod with the same image. The control plane must remain stable.
Missing lifecycle data is unknown. A correct HTTP response after failed parking
is not a qualified wake-up. Both offered and configured cycle counts are retained.

## Artifacts and offline checks

The run contains `protocol.json`, frozen corpus/oracle in `inputs/`, and one
folder per treatment. Each arm saves flushed request ledgers (including started
attempts, full response bodies, failures and monotonic durations), raw Kubernetes
observations, cycle records, a summary and labelled Prometheus series. Existing
NanoLab run provenance additionally records the environment and source metadata.

```sh
python -m nanolab.tasks.idle_reactivation_report /data/results/b6-java-block01
```

This regenerates `paired-summary.json`, checks archived response bodies against
the frozen oracle, and returns failure for incomplete or unqualified pairs.
Memory reports retain working set and RSS separately, with control-plane/function
components, idle means and MiB-seconds. Missing whole-cycle coverage remains null.
Only verified pod absence plus healthy cAdvisor permits zero function memory;
stale old-pod samples are excluded. The 5-second query grid overlays 10-second
cAdvisor scrapes. Integration uses in-window samples, at most 5-second boundary
holds and no gaps above 10 seconds. These are container metrics, not whole-VM RAM
or startup memory peaks. Latency runs from client send through full response body,
using a fresh connection in both arms. No cross-SDK initialization-time comparison.

The five-runtime pilot has 120 minutes of cumulative idle time plus about 10
minutes of warmup, builds, provisioning and requests. Pilot data validate the
method; they are excluded from the final campaign. Final repetition counts and
paper plots are frozen after inspecting the pilot, not generated from synthetic
test results.
