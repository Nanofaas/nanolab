# B1: a controlled concurrency-controller comparison

Use `concurrency-comparison-{adaptive,sojourn,budgeted}.yaml`. The parsed scenarios
are identical except for `concurrencyMode`; a regression test enforces that.
All three use Kubernetes, two fixed-replica word-stats functions (Java and
Java-lite), the same medium corpus, and the same open-loop arrival schedule.
No autoscaler or sync-queue admission estimator is enabled. Each function has
2 CPU / 512 MiB limits (requests: 1 CPU / 256 MiB); the control plane has
2 CPU / 2048 MiB limits (requests: 250m / 512 MiB). Queue capacity is 100 per
function, invocation timeout is 30 seconds, and the existing retry policy is
unchanged. No Kubernetes shared-CPU affinity is claimed.

The per-function concurrency ceiling is 8, so the independent controllers can
collectively grant at most 16. `concurrencyTotalBudget: 16` pins BUDGETED's global
upper bound to that same maximum instead of deriving it from detected cores.
This pilot compares controllers at equal maximum capacity. At this budget the
global allocator is not forced into scarcity; it cannot establish the benefit
of reallocating a tighter shared budget. That requires a separate protocol with
an equivalent shared admission cap for all modes, not silently lowering only
BUDGETED's budget.

The existing open-loop calendar is unchanged: function A runs for 360 seconds,
function B starts at 120 seconds and ends at 360 seconds, with staggered and
simultaneous peaks. NanoLab passes a peak of 1800 arrivals/s per function.
The declared 5000 VUs per function are generator capacity, not offered load.
The medium corpus is shared between VUs, uses exactly the same request inputs
for the two word-stats implementations, and is resolved inside the synchronized
repository on the loadgen VM. Missing/empty corpora fail rather than falling
back to tiny built-in sentences.

Controller-specific parameters remain explicit in generated function
registrations: ADAPTIVE uses its existing gradient settings and 5-second
cooldowns; SOJOURN uses the payload-specific 120 ms caller-latency target;
BUDGETED uses the existing 10 ms service-time target and equal weights. Those
are different controller signals, not equivalent latency objectives. Do not
retune one arm after seeing its result.

Run all three with NanoLab's `run` command and a managed Azure Spot environment,
using distinct names from other campaigns. Each arm uses a fresh dedicated VM
pair of the same SKU, created and released by NanoLab itself (no --keep). This
retains the workflow-owned lifecycle; cloud-host variation remains a limitation.
A pilot uses one run per mode, in declared order; later repetitions should
balance order. Use an 8-vCPU/32-GiB stack and an 8-vCPU/32-GiB loadgen to keep
builds and generator capacity separate from measured container limits.

Inspect per-function concurrency trajectories, service time, queue wait/depth,
HTTP outcomes, generator drops and resources. The legacy k6 `served` and
`refused` checks are mutually exclusive; their combined pass fraction is not a
correctness or success rate. Preserve failed acceptance checks and partial raw
results. A planning pass does not prove the governor responds; runtime results
must establish that the workload actually exercises it.

On the operator VM, set NANOLAB_WORKSPACE and TMPDIR under /data and pass an
explicit /data run directory for every arm. Fetch results before workflow-owned
VM release. Do not interpret an absent remote VM as loss of the controller copy.

## B1.1: fixed baselines

B1.1 reruns the three controllers together with fixed limits 1, 2, 4 and 8.
The four `concurrency-comparison-fixedN.yaml` scenarios differ from the common
adaptive scenario only by mode FIXED and the explicitly selected fixedConcurrency.
FIXED is an existing NanoFaaS mode, not a new scheduling algorithm. Modules,
images, resource limits, replicas, corpus, arrival schedule, queue capacity,
timeouts and retries are held constant. Function registration uses N as its
configured concurrency; the adaptive arms retain the ceiling of 8. The fixed
runs record the same per-function telemetry but validate a constant limit and
observed work rather than requiring the controller to change its target.

The initial B1 run failed before measurement because the legacy Java-lite
Docker contexts omitted the logging-runtime Gradle project. Correct all three
Java-lite contexts together, preserve the failure evidence, and freeze that fix
for every B1.1 arm. Do not append fixed baselines to an incomplete B1 run and
present them as a completed seven-arm comparison. A single-run B1.1 pilot is
still preliminary; use repeated, balanced orders before paper-level inference.
