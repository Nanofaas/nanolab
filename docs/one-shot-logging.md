# Optional logging in one-shot workflows

Set `oneShot.observability.logs.enabled: true` in calibration, qualification and
experiment scenarios. Legacy scenarios create no collector/backend resources
and retain their previous profile fingerprint. Example:

```yaml
oneShot:
  observability:
    logs:
      enabled: true
      endpoint: http://10.83.80.1:3100/loki/api/v1/push
      image: fluent/fluent-bit:5.1.3
      cpu: 0.10
      memoryMiB: 64
      bufferMiB: 256
      environment: local
      campaignId: my-campaign
      backendOwned: false
      exportLimit: 5000
```

The endpoint must be reachable from every VM. An existing backend is never
created, stopped or deleted. For an owned backend on the operator host, set
`backendOwned: true` and `backendBind` to that host's private VM-facing IPv4 address.
Loki/Grafana use NanoFaaS's `deploy/observability/compose.yaml`, persistent named
volumes, fixed images, CPU 1.5 and RAM 768 MiB in total; reserve those resources
outside the VMs. Collector CPU/RAM must fit each node's compute/memory budget.
Backend disk reserve includes 72-hour retention and must be monitored separately.

Use a NanoFaaS source tree containing the logging deployment assets. Every VM
uses an independently owned Fluent Bit container and persistent state volume.
Docker `json-file` is required for managed function capture; the agent itself
uses `local` to avoid recursive collection. Control-plane and function
specifications select JSON, but the SDK's separate one-shot measurement protocol
is unchanged. Collectors add node, campaign, run, mode and role metadata; request
IDs are JSON fields, never Loki labels. Unknown epoch metadata is not inferred.

Sonata keeps the collector alive through its platform/function consumers.
On teardown it allows at most five two-second drain polls (each metrics request
has a three-second timeout), then stops the collector with a 15-second grace
period. It archives the stopped collector's SQLite state and buffered chunks to
`logging/<nodeId>/collector-state.tar.gz` and verifies the transferred SHA-256
before removing its state volume. This archive can retain records not yet in Loki;
replay is an operator recovery action using the same collector configuration and
can duplicate delivered records. It is not an automatically replayed log archive.

Metrics, resource snapshots, collector logs and bounded Loki query results are
also retained with hashes. `status.json` separates `exportStatus` from `collection`
delivery counters and `stateArchive`. Missing counters, pending records, known
drops, failed query/archive or truncation produce `partial`; a successful query
alone cannot prove delivery. Counters describe records observed by the collector,
not proof that every source line was observed. Archive failures are explicit run
errors even though cleanup of owned ephemeral resources still runs; disk failure
can prevent preservation. Export failure does not rewrite SDK completions or
error accounting. The per-node query cap is 5000 entries, so
large runs need external retention/query export as well. This is not a complete
archive promise.

Preflight records actual collector image IDs, staged configuration hashes and
container CPU/RAM limits. Enabled logging requires these facts in the profile
fingerprint. Changing format, endpoint, image/config/budget invalidates profile
compatibility; campaign/run IDs do not. External backend upgrades require explicit
requalification, as NanoLab does not own their version or resource limits.

Before calibration, run the paired interference gate. The reproducible
`scripts/one-shot-logging-overhead.py` pilot owns six heterogeneous Multipass VMs,
uses existing Sonata bootstrap/lifetimes, identical CPU-bound input and fixed
arrival rates for three off/on pairs. It records raw k6 samples, HTTP goodput/p95,
generator deficit and cumulative CPU/block-I/O counters bracketing each measured
window for workloads, collectors and owned Loki/Grafana. Mean cores use that
bracketing interval (including sequential sampling overhead); RAM readings are
window endpoints, not peaks. Supplementary Docker snapshots are also retained,
with all owned VM cleanup
verified. Supply the tested NanoFaaS native/function image archive and private
host endpoint. This pilot has one replica per node and P2P disabled: it does not
qualify the complete scientific campaign. Repeat the gate on the frozen campaign
stack/topology/replica counts before publishing calibration profiles. Preserve
failed gates; never select only favorable trials.

The pilot keeps both nominal rate × duration and actual generated/completed
counts. k6 can schedule a boundary iteration; success requires every actual
request to complete, no dropped iterations and no deficit against the nominal
count. Its checksum comparison ignores the changing invocation envelope and
compares the successful u64 output without JavaScript number precision loss.

Protocol v2 spreads each fixed arrival rate across the CPU quota period with
identical deterministic 0–100 ms phase offsets in all windows. The original
fixed-phase series failed the 5% p95 gate and remains archived; its tightly
clustered per-node timings shifted between windows. This protocol revision
does not change images, payloads, rates or logging budgets.
