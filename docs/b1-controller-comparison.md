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
using distinct names from other campaigns. Keep the same dedicated stack and
loadgen VMs across arms and release them through NanoLab after the campaign.
A pilot uses one run per mode, in declared order; later repetitions should
balance order. Use an 8-vCPU/32-GiB stack and an 8-vCPU/32-GiB loadgen to keep
builds and generator capacity separate from measured container limits.

Inspect per-function concurrency trajectories, service time, queue wait/depth,
HTTP outcomes, generator drops and resources. The legacy k6 `served` and
`refused` checks are mutually exclusive; their combined pass fraction is not a
correctness or success rate. Preserve failed acceptance checks and partial raw
results. A planning pass does not prove the governor responds; runtime results
must establish that the workload actually exercises it.
