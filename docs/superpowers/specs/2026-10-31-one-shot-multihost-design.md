# One-shot across two DGX Sparks with an Azure cloud

Status: draft v2 for review, 2026-10-31 (v1 corrected after reading the code
paths and the reference run's scenario).

## Goal

Run the existing one-shot observation sequence (step ORACLE, continuous ORACLE,
continuous ARI) on a topology spread over several physical sites:

- edge VMs on two identical DGX Sparks (`149.132.179.35`, `149.132.179.175`),
  five per Spark, each five with the node configuration and memory pools of
  today's single-Spark layout;
- one terminal cloud VM on Azure Spot, x86 (`Standard_D8s_v5`), deliberately a
  different architecture from the edges;
- the nanolab controller and the k6 generator on the operator host
  (`titan-inside`), which owns no experiment VM.

Edge processing times are **not** recalibrated: the Sparks and the per-node VM
configuration are identical to the calibrated layout. The cloud is **not**
calibrated: cloud processing times are not used by the tuning or the analysis.

## Phases

`.175` sits behind a router that frequently stops forwarding, so:

1. **Implement and validate on `.35` + Azure.** The reference scenario's five
   edges on `.35`, cloud on Azure, controller and k6 on the operator. The code
   is the multi-site code with one Spark listed.
2. **Extend to `.175`.** A ten-edge scenario. This is *not* configuration only:
   the frozen trace and `selected.json` of the reference cover `edge-1..5`, so a
   ten-edge load has to be produced first (see Open decisions).

### Phase 1 validation against the ARI reference

Scenario, functions, trace and `selected.json` equal the completed reference run
`one-shot-sequence-retries3-20261030-103102/ari-retry-20261030T152645Z`
(continuous ARI, archive verified). Edge images are byte-identical. What differs:
cloud architecture and location, controller/k6 location, and every edge path now
crosses a user-space forwarder. Only edge-side behaviour is expected to match:

| Must match the reference (within run-to-run noise) | May differ; recorded, not judged |
|---|---|
| Frozen plan, per-window grants and peer offload | k6 latency p50/p95 (extra network hops) |
| Queue admissions, rejects and retries at the edges | Service time and throughput of cloud-served requests |
| Edge completions and throughput | RTT operator→edges, edges→cloud; peer RTT via forwarder |
| Verified archive, no anomalies, clean release | |

A small comparison script produces this table from the two archives; it is part
of the work, not a manual step.

## Fixed experiment settings

Same as the reference sequence (`runtimeRevision c0d15e3d`):

- queues enabled on edges and cloud: function capacity 100, global 200,
  `maxEstimatedWaitSeconds` 2, `maxQueueWaitSeconds` 2, and the preflight that
  requires a real invocation to increment `sync_queue_admitted_total` on every
  node before the schedule freezes;
- `max_retries=3` in the observation plan (commit `16ebfc42`), existing
  exponential jitter backoff (100 ms initial, 2 s cap);
- edge logging enabled as in the reference (fluent-bit collectors, owned Loki
  backend), see Logging;
- the patched Multipass client the sequence used (upstream PR 5485, today at
  `/tmp/nanolab-multipass-fixed/bin/multipass` on `.35`), installed on every
  Spark at a stable path.

## Topology

Node ids are global. For phase 2, `edge-6..10` copy `edge-1..5` exactly
(1/1/2/2/3 vCPU, the same function limits, `memoryCapacityMiB` and RAM).

| Site | Nodes | Intra-site graph |
|---|---|---|
| `.35` | `edge-1..5` | two triangles sharing `edge-3`: 1-2, 1-3, 2-3, 3-4, 3-5, 4-5 |
| `.175` | `edge-6..10` | star on `edge-10`: 10-6, 10-7, 10-8, 10-9 |
| cross-site | | 1-6, 5-8 (deliberately asymmetric) |
| Azure | `cloud` | outside the peer graph; terminal offload target |

`validate_neighbor_graph` (connected, symmetric, edge-only) accepts this graph.
Every edge seeds every other edge (as today); non-neighbors are `EXCLUDED`.

## Network

### What has to flow

Each node exposes four TCP services inside its VM, plus dynamic replica ports:

| VM port | Service | Used by |
|---|---|---|
| 8080 | control plane API, invocations | operator (control, k6), peers (invocation URI), edges → cloud |
| 9090 | management: readiness, `/actuator/prometheus` | operator |
| 14200 | p2p gossip (scalecube TCP) | peers |
| 17111 | clock/inventory/bandwidth probe | operator (clock), other nodes (bandwidth) |
| dynamic | function replicas (`HostPort` of each container) | operator reads physical release proofs and inventories |

The cloud initiates no connection during the run (offload is synchronous HTTP;
rejects travel in the response; p2p excludes the cloud). One preflight step
violates this today: the bandwidth check makes each node download from *another*
node's probe, and for the cloud that other node is an edge. It is reversed for
the cloud (an edge downloads from the cloud's probe).

### Publishing Spark VMs without a tunnel

Multipass VMs stay behind each Spark's NAT. Each node's four services are
published on its Spark's LAN address by user-space `socat` forwarders on
unprivileged ports. Verified on `.35`: firewall inactive, the operator reaches
high ports directly with its source address preserved; `socat` 1.8.0.0 is
installed. WireGuard was rejected (needs root on the operator host, a Proxmox
node where route mistakes can isolate its VMs); Ansible does not remove the
need for root (`become`).

- **Per-node ports on the Spark:** `edge-n` gets `18000+n` (8080),
  `19000+n` (9090), `14200+n` (gossip) and `17100+n` (probe). The gossip bind
  port inside the VM is set to `14200+n` too, because scalecube advertises its
  bind port; the other three keep their VM ports.
- **Advertised addresses:** `p2p.external-host=<spark IP>`,
  `p2p.invocation-uri=http://<spark IP>:18000+n`, seeds
  `<spark IP>:14200+m`, and the cloud URI passed to edges is the cloud's public
  endpoint. nanoFaaS `c0d15e3d` supports all of these unchanged.
- **Replica ports:** dynamic `HostPort`s cannot be forwarded statically. The
  in-VM probe gains a relay route restricted to `127.0.0.1` and to ports of
  containers labelled `io.nanofaas.managed=true`; the operator reads replica
  proofs and inventories through the probe's published port. The relay is on
  the evidence path only, never on the measured invocation path.
- **Forwarders are a run resource** (`socat TCP-LISTEN:<p>,fork,reuseaddr
  TCP:<vm ip>:<port>`), started over SSH as the operator user once the VM has
  an address, stopped on release (success or failure), modelled as a Sonata
  `Resource` like `sonata_tasks.registry_tunnel`, kept in nanolab until stable
  and then moved to `sonata-tasks`. Preflight refuses a port already in use.
- **Consequence to measure:** every peer and k6 path now crosses the Spark's
  host address and a forwarder instead of the Multipass bridge.

### Explicit node addresses

Today several call sites derive the management URL with
`endpoint.replace(":8080", ":9090")` (`observation.py`, `queue_telemetry.py`,
`experiment.py`, `qualification.py`) and probes/readiness use `vm.host`
directly. With forwarded ports that replacement silently produces a wrong URL.
All of them move to an explicit per-node address record
(`api`, `management`, `gossip`, `probe`, `advertisedHost`) produced by the
placement layer: VM address and fixed ports for a local Multipass node, Spark
address and forwarded ports for a remote one, public IP for Azure.

### Azure

- NSG inbound from the operator's egress IP: 22, 8080, 9090, 17111 (and the
  log backend port, see Logging); from each Spark's egress IP: 8080 and 17111.
  Egress IPs are discovered at preflight (`.175`'s may differ from its LAN
  address). Uses Sonata's `restrict_inbound_sources`.
- The provider's template also opens SSH from `*` (rule 1001); acceptable for
  a short-lived run VM, noted as a follow-up in Sonata.

## Placement and remote execution

### Environment

```yaml
provider: multisite
sites:
  spark-35:
    kind: multipass-ssh
    ssh: 149.132.179.35
    address: 149.132.179.35
    multipass: ~/.local/share/nanolab/multipass-fixed/bin/multipass
  spark-175:
    kind: multipass-ssh
    ssh: 149.132.179.175
    address: 149.132.179.175
    multipass: ~/.local/share/nanolab/multipass-fixed/bin/multipass
  azure:
    kind: azure
    resource_group: maurinoRicerca-rg
    location: westeurope
    priority: Spot
    eviction_policy: Delete
    max_bid_price: -1
    vm_size: Standard_D8s_v5
    image_urn: Canonical:ubuntu-24_04-lts:server:24.04.202610210
    ssh_key_path: ~/.ssh/id_ed25519_azure_nanolab.pub
    tofu_workspace: /datassd/nanolab-twospark/tofu
placement:
  edge-1: spark-35
  # ...
  cloud: azure
```

Placement must name exactly the scenario's nodes. The phase-1 environment
places the reference scenario's six nodes; the phase-2 environment places the
ten-edge scenario's eleven. `OneShotConfig.provider` accepts `multisite`; the
existing `multipass` (local) path is unchanged.

### One node shell for every remote operation

Local `multipass` calls are spread beyond `infrastructure.py`: the cloud-init
wait in `_bootstrap`, the long-running VM sampler stream in `vm_resources.py`,
the raw-recovery `multipass stop`, `external_vm_cpus`, and the observe script's
`multipass list` inventory (which also hard-codes a multipass environment).
They all go through one per-node interface with `exec`, `stream` (long-running,
line output), `transfer_to`, and site-level `inventory`:

- **local Multipass** — today's behaviour;
- **Multipass over SSH** — `ssh <site> <multipass> exec|transfer|…`;
  `transfer_to` stages a file once per site, then `multipass transfer`;
- **Azure** — the Sonata `AzureVmProvider` (SSH to the VM).

### Per-site capacity and CPU placement

The reference pins every VM's QEMU process to fixed Spark CPUs with a frozen
max frequency, and pins the operator (k6, nanolab) and the logging backend:

- `hostCpuPlacement` becomes per site: edge pins, logging CPUs and frequency
  checks are evaluated and applied on the Spark over SSH (`sudo -n taskset`,
  which already works on `.35`: `michele` has passwordless sudo there; `.175`
  needs the same); operator CPUs are validated and pinned on `titan-inside`
  (governor `performance`, 40 CPUs, low load); the cloud has no host placement.
- `host_budget`, `external_vm_cpus` and capacity checks run per Spark against
  that Spark's facts.

Statement correction from v1: the run needs no root for *networking*; CPU
pinning on the Sparks uses `sudo -n` exactly as the single-Spark workflow does.

### Azure cloud lifecycle

Reuses the Sonata `AzureVmProvider` with a run-scoped name
`nl-os-<run>-cloud`. The provider drives **OpenTofu** (`tofu`) and deletes only
what its workspace recorded, so `tofu` (OpenTofu, same major as `.35`'s 1.13) is
installed under `/datassd/nanolab-twospark/bin` and the workspace lives under
`/datassd`. Each VM gets its own vnet/subnet/NSG; the resource group is a data
source, so the shared `maurinoRicerca-vnet` and the unrelated VMs (one is named
`nanolab`) are never touched.

Spot eviction marks the run **failed**, no recovery. Detection: the existing
supervision fails on lost cloud health; release then records the Azure VM state
(evicted or deleted) in the run receipt. Scheduled-events polling would have to
run inside the VM and is not needed for "failed on eviction".

## Logging

The reference runs fluent-bit on every node and an owned Loki backend bound to
the Multipass bridge of `.35` (`10.83.80.1:43100`); queue-event export reads it.
With the operator off-site, a single backend would make collectors push across
sites during the run, perturbing the network being measured, and the cloud
cannot reach the operator. So the backend is **per site**:

- one Loki per Spark, bound to that Spark's bridge address, run with the Spark's
  Docker, pinned to that Spark's logging CPUs;
- one Loki on the cloud VM itself, bound to localhost;
- the operator queries each backend through its published port (forwarder on the
  Spark, NSG on Azure) and exports per site, keeping the existing per-node
  identity checks.

## Images

Built once, reused until the nanoFaaS revision or recipe changes.

- **Edges (arm64):** the reference sequence's images exported from `.35`
  (`sequence-c0d15e3d-20261029220906`; control plane `7b29fcb6…`, shared Rust
  function image `469035c5…`), byte-identical to the ARI reference.
- **Cloud (amd64):** built once on a short-lived Azure Spot VM (Ubuntu 24.04,
  Docker classic image store, OpenJDK 25.0.4.1 as on `.35`) from a git bundle
  of `c0d15e3d`, recipe byte-identical (`d0905161…`); tag
  `sequence-c0d15e3d-amd64-202610311934`. The classic store matters: with the
  containerd store image IDs are manifest digests and would not match after
  `docker load`.
- Done: `/datassd/nanolab-twospark/images/{arm64,amd64}/` with archives,
  `distribution.json` per architecture and `manifest.json` (hashes, IDs, tags,
  source revision, provenance).
- Code: `runtimeDistribution` becomes per architecture; `save_images` (local
  Docker) is replaced, for archives given in the manifest, by verify → transfer
  the node's architecture archive → `docker load` → existing loaded-ID check.
  The operator host needs no Docker.

## Operator host

All data under `/datassd/nanolab-twospark/` (`env.sh`: `NANOLAB_WORKSPACE`,
`UV_CACHE_DIR`, `TMPDIR`, `PATH`); `pve-root` is nearly full. k6 v2.2.0 (as on
`.35`) installed. The sequence's historical inputs (observations, physical
samples, tuning directories, parent freeze) are copied from `.35` with their
hashes; freezes are prepared on the operator (host identity recorded there).
Run archives are copied to `/mnt/pve/qnap` after verification. The operator is
a Proxmox node: the run records its load and pins k6/nanolab to dedicated CPUs.

## Azure authentication

The tenant requires MFA for writes; a login answering the claims challenge
works. Whether the session survives a multi-hour sequence depends on the
tenant's sign-in frequency policy, not on the access-token lifetime. Preflight
checks that a token can be issued; before scientific sequences, verify the
policy or use a service principal scoped to `maurinoRicerca-rg`.

## Failure handling

- Preflight before any acquisition: SSH to every site within a bound; `socat`,
  patched Multipass, free published ports and passwordless `taskset` on each
  Spark; disk headroom per Spark; `tofu`, Azure token and Spot quota; egress
  IPs discovered.
- Every remote operation has an explicit timeout. If a site disappears mid-run
  the run fails; release continues per site and records any VM or forwarder it
  could not remove as an orphan in the run receipt.
- Spot eviction: run failed.

## Out of scope

- Recalibrating edge or cloud processing times.
- Automatic network setup requiring root.
- Cloud participation in p2p discovery.
- Recovering a run after eviction or site loss.

## Testing

- Unit: environment parsing and placement; port allocation and advertised
  addresses; node address record replacing `replace(":8080", ":9090")`;
  SSH-wrapped Multipass argv; per-site capacity and placement; per-arch archive
  selection and ID checks; probe relay restrictions; NSG port/source sets;
  ten-edge graph.
- Integration on `.35` without Azure: one VM, forwarders up/down, gossip and
  invocation through forwarded ports, probe relay.
- Phase 1 sequence on `.35` + Azure, compared with the ARI reference.
- Phase 2 on both Sparks once `.175` is reachable.

## Open decisions

- **Ten-edge load (phase 2):** duplicate the reference per-node rates onto
  `edge-6..10`, or re-run the load tuning for the ten-edge graph.
- **Azure session policy** for multi-hour sequences (see above).
- `.175`: `socat`, patched Multipass, passwordless `taskset` and high-port
  reachability through its router, checked when it is back.
