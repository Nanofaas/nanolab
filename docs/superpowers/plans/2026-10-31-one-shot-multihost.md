# One-shot across two DGX Sparks with an Azure cloud — plan

Spec: `docs/superpowers/specs/2026-10-31-one-shot-multihost-design.md` (v2).

Principles: the existing local-Multipass path keeps its behaviour and its tests
(regression first); every new remote path is unit-tested with fake shells before
touching a Spark; nothing is written to `pve-root` (`source
/datassd/nanolab-twospark/env.sh`, pytest `--basetemp` under `/datassd`).
Baseline on `main` (`16ebfc42`): 48 failures, all `FileNotFoundError: 'node'`
(no Node.js on the operator); anything else failing is ours.

Checks per task: targeted pytest, `ruff check packages`, `basedpyright --project
packages/nanolab`, `lint-imports` (CLAUDE.md commands). One commit per task.

## 0. Prerequisites (no code)

- [x] Images: `images/{arm64,amd64}` + `manifest.json` (done 2026-10-31).
- [x] k6 v2.2.0 on the operator; `env.sh`; uv cache/TMPDIR on `/datassd`.
- [x] OpenTofu on the operator 1.13.2 (as `.35`); `TF_PLUGIN_CACHE_DIR` and the
      provider workspace under `/datassd/nanolab-twospark/tofu`.
- [x] Patched Multipass client (PR 5485) on `.35` copied from `/tmp` to
      `~/.local/share/nanolab/multipass-fixed`, wrapper paths updated, version
      recorded (sha `b615fd32…`, 1.17.0-dev.pr5485).
- [x] Copy the sequence's historical inputs from `.35` (observations, physical
      samples in their `physical-window` directory, step/continuous tuning,
      parent freeze) to `/datassd/nanolab-twospark/inputs` and verify hashes
      with `one-shot-sequence.py --check` (passes; runtime source is a clean
      `c0d15e3d` clone at `/datassd/nanolab-twospark/nanofaas-c0d15e3d`).
- [x] Egress IPs equal LAN addresses: operator `149.132.176.50`, `.35`
      `149.132.179.35`.

## 1. Configuration model

- [x] `EnvironmentConfig`: `provider: multisite`, `sites` (`multipass-ssh`,
      `azure`), `placement`; validation: placement covers exactly the scenario
      nodes, edges only on Multipass sites, one cloud.
- [x] `OneShotConfig.provider` accepts `multisite`; `hostCpuPlacement` per site
      (edge pins and logging CPUs per Spark, operator CPUs on the operator).
- [x] `runtimeDistribution` per architecture (path + sha256 each), validated
      against `images/manifest.json`.
- [ ] Presets: `multisite-phase1.yaml.example` (reference scenario's nodes on
      `spark-35` + `azure`); phase-2 example once the ten-edge load exists.
- Tests: parsing, every validation error, old `multipass` configs unchanged.

## 2. Explicit node addresses

- [x] `NodeAddresses(api, management, gossip, probe, advertised_host)` from the
      placement layer; port allocation `18000+n`, `19000+n`, `14200+n`,
      `17100+n` for remote Multipass nodes; VM address + fixed ports locally.
- [x] Replace every `replace(":8080", ":9090")` (`observation.py`,
      `queue_telemetry.py`, `experiment.py`, `qualification.py`) and every direct
      `vm.host` URL (readiness, probe, preflight) with the record.
- [x] Control-plane args: `p2p.port`, `external-host`, `invocation-uri`, seeds
      from the record.
- Tests: local path yields today's exact args/URLs (golden); remote path yields
      forwarded ones; no `:8080`→`:9090` string surgery left (grep test).

## 3. Node shell

- [x] Interface `exec`, `stream`, `transfer_to`, site `inventory`; implementations
      local Multipass (today), Multipass-over-SSH (`MultipassClient(cmd=…,
      backend=SshBackend)`, staged `transfer_to` once per site), Azure (Sonata
      provider).
- [x] Route through it: `_bootstrap` cloud-init wait, VM sampler stream,
      raw-recovery `multipass stop`, `external_vm_cpus`, observe-script
      inventory; remove the hard-coded multipass `EnvironmentConfig` in
      `scripts/one-shot-observe.py` (environment becomes an argument).
- Tests: argv golden tests per implementation; timeouts on every remote call;
      SSH failure surfaces as a failure, not as empty output.

## 4. Remote Multipass lifecycle, capacity and CPU placement

- [x] VM acquire/release on a Spark through the SSH-backed provider; cloud-init
      file staged on the Spark; refuse to adopt; release in reverse order.
- [x] Capacity, `host_budget`, `external_vm_cpus` per site from remote
      `/proc/meminfo`, `nproc`, `multipass info`.
- [x] Host CPU placement per site: remote `/sys` facts and governor, remote
      QEMU PID discovery and `sudo -n taskset`; operator pinning stays local.
- Tests: fake remote shell with recorded `/proc`/`/sys` fixtures from `.35`.

## 5. Forwarders

- [x] Sonata-style `Resource`: start one `socat` per published port over SSH
      (own process group, PID file under the run dir on the Spark), verify
      listening, stop on release and compensation; preflight refuses busy ports.
- [x] Ordered after the VM has an address, before the control plane starts.
- Tests: argv, release after partial start, busy-port refusal.
- Integration on `.35`: one throwaway VM, forward `:8080`, reach it from the
      operator, release leaves no `socat`.

## 6. Probe relay and replica access

- [x] `assets/one-shot/probe.py`: `/relay/<port>/<path>` to `127.0.0.1` only,
      ports restricted to running `io.nanofaas.managed=true` containers.
      Probe hash is part of the evidence; record the new hash.
- [x] `runtime_inventory` / `runtime_endpoint` return relay URLs for remote
      nodes; physical-proof reads use them.
- [x] Bandwidth preflight: for the cloud, an edge downloads from the cloud's
      probe (no cloud-initiated connection).
- Tests: relay refuses non-managed ports and non-local targets; local path
      unchanged.

## 7. Azure cloud node

- [x] `VmRequest` for the cloud from the `azure` site (Spot, Delete, size,
      image, key, tofu workspace on `/datassd`).
- [x] NSG via `restrict_inbound_sources`: operator → 22/8080/9090/17111/log
      port; Spark egress → 8080/17111.
- [ ] Release receipt records eviction/deletion state; preflight checks token,
      quota, `tofu`.
- Tests: request/NSG composition; receipt on simulated eviction.
- Integration: create/verify/delete one cloud VM alone (MFA login needed).

## 8. Images per architecture

- [x] Archive resource from `images/manifest.json`: verify archive hash and
      config digests == distribution IDs; per node pick by architecture; stage
      once per Spark, `docker load` in each VM, existing loaded-ID check.
- [x] Keep `save_images` for configs without a manifest (local path).
- Tests: wrong-arch refusal, ID mismatch refusal, staging once per site.

## 9. Logging per site

- [x] Backend resource per Spark (Docker on the Spark, bound to its bridge,
      pinned to its logging CPUs) and on the cloud VM (localhost).
- [x] Collectors point to their site's backend; queue-event export per site
      through the published port.
- Tests: endpoint selection per site; export merges per-node identities.

## 10. Sequence and observe scripts

- [x] `one-shot-sequence.py`: accept prebuilt per-arch distributions (skip
      `assembleRecipe`), an environment file and the copied inputs; record
      sites, versions and egress IPs in `sequence.json`.
- [x] `one-shot-observe.py`: environment argument; per-site inventories
      before/after.

## 11. Comparison with the reference

- [x] `scripts/one-shot-compare.py <reference archive> <run archive>`: the
      spec's must-match / may-differ table as JSON + Markdown.

## 12. Phase 1 runs

- [ ] Dry run: phase-1 environment, `--check` paths, preflight only.
- [ ] One continuous-ARI run on `.35` + Azure; compare with the reference.
- [ ] If it matches: full sequence (step ORACLE, continuous ORACLE, continuous
      ARI). Archives to `/mnt/pve/qnap`.

## 13. Phase 2 preparation

- [ ] User decision on the ten-edge load (duplicate rates vs re-tune).
- [ ] `.175`: `socat`, patched Multipass, `sudo -n taskset`, high ports through
      its router, disk.
- [ ] Ten-edge scenario + environment; dry run; first run.

## Implementation notes (2026-10-31)

- Environment keeps `provider: multipass` and gains `sites` + `placement`
  (site `architecture` explicit; Azure `architecture`, `work_dir`); no new
  provider name, so the reference scenario is reused unchanged.
- Published ports: API `18000+n`, management `API+1010` (same offset as
  8080→9090, so one `management_url()` serves every case), gossip `14200+n`,
  probe `17200+n`; `n` is the node's position in the scenario.
- Remote Multipass reuses `VmOrchestrator` with an SSH command backend and an
  SSH `ProxyCommand` through the site (shared control socket); bootstrap
  Ansible gets the same jump. Ansible on the operator: `ansible-core 2.16.3`
  (as on `.35` for the reference), although `requirements.txt` says 2.20.
- Forwarders are tied to the node VM's lifecycle (start after pin, stop
  before teardown) rather than a separate resource.
- Azure: `manage_resource_group=False` so the shared group never enters
  OpenTofu state; workspace under `/datassd`.
- Logging per site: Loki on `.35`'s bridge with its Docker (forwarder `43200`
  for the operator); Loki inside the cloud VM (`docker-compose-v2`, `loki`
  only, private IP). Deviation: the cloud's Loki shares the cloud VM's 8 vCPUs.
- Integration smoke: `scripts/one-shot-multisite-smoke.py <freeze> <out>`.
- Azure disk is at least 64 GiB (`AZURE_MIN_DISK_GIB`): the Ubuntu image is
  30 GiB, larger than the scenario's 20 GiB node disk.
- Sonata names NSG rules `Port<n>`, so a port opened twice overwrites its
  rule: cloud ingress groups ports so each appears in one call (operator-only
  22/9090/Loki; operator + site addresses for 8080/17111).
- Smoke results (2026-11-01, phase 1, 5th attempt, passed): exact two-triangle
  peer graph through published gossip addresses; edge clock offsets
  1.6–2.1 ms ±0.5–0.7, cloud 1.1 ±10.3 ms; operator RTT 1.0–1.4 ms to edges,
  20.5 ms to cloud; edge bandwidth 2.5–3.1 Gbit/s, edge→cloud 241 Mbit/s;
  both Loki ingestion probes passed; full release verified on `.35` and Azure
  after every attempt.

## Changes after the first validation attempt (2026-11-01)

- The phase-1 ARI run failed while acquiring the cloud control plane: one
  `docker compose ps` of the cloud's Loki, over a fresh SSH connection,
  failed and the supervisor aborted. The cause was not recorded: the
  background worker reported only "one-shot clock monitor failed". Failures of
  background probes now carry their cause (type, exit status, stderr).
- SSH to the cloud shares one connection (ControlMaster, keepalive,
  connect timeout) like the sites; long-lived tunnels use their own.
- Loki per site is replaced by one Loki on the operator: the release binary
  (3.7.8, the compose image's version, sha256-pinned in the environment's
  `logging` block), on the operator's loopback, pinned to its own CPUs. The
  operator has no Docker and is reachable from the sites only on a public
  address, so the backend stays on loopback (the scenario's private-bind rule)
  and each site and the cloud reach it through `ssh -R`: a site publishes the
  tunnel on its first Multipass bridge with socat (the scenario's endpoint, so
  the edges push where they did in the reference), the cloud's collector (host
  network) pushes to the tunnel's loopback end. A tunnel that exits is reopened
  and recorded in `logging/tunnels.jsonl`; Fluent Bit buffers on disk with
  unlimited retries, and per-collector delivery is still verified.
- The cloud's NSG rules are declared at creation through
  `VmRequest.azure_ingress` (Sonata 0.6.20, azure-vm-sdk 0.9.0) instead of
  being rewritten afterwards; no Loki port is opened on the cloud any more.
- The integration smoke runs under the campaign supervisor and can `--hold`,
  so the periodic checks run as in an observation.

## Follow-ups (recorded 2026-11-01, third phase-1 run)

Timing (a phase-1 run: provisioning ~18 min, warm-up ~50 min, load 66 min,
collection and release ~15 min; with ten edges the serial parts double):

1. Warm replicas of different nodes in parallel. `WarmInstances` walks every
   replica of every node in turn; through the relay, NAT and WAN the 80
   replicas took ~50 min against ~28.5 min in the single-host reference.
   Nodes are independent, so one worker per node bounds it by the slowest node.
2. Provision sites and the cloud in parallel. VM creation (~37 s each) and
   per-node bootstrap, collector and control plane (~1.5 min each) run one
   after another, and the cloud (~4 min) waits for all edges. Needs either
   parallel acquisition in Sonata or one resource per site that starts its
   nodes concurrently while keeping per-node release.

Robustness:

3. The smoke must exercise every write path of the observation. It checked
   only reads, so the relay refusing `POST invoke` surfaced 20 minutes into a
   real run. Run the preflight's warm-up (and the queue admission check) in
   the smoke, not only topology collection.
4. Recognise a Spot eviction explicitly. The design says eviction fails the
   run, but nothing detects it: it would surface as an unreachable node. Read
   the VM's eviction state on failure and record it as the cause.
5. Bound the Azure session. Long runs rely on `az` refreshing the token and
   the release needs a write; check the remaining session at launch, or use a
   service principal for runs.
6. Inject faults in the smoke: kill a log tunnel during `--hold` and check it
   is reopened with no records lost (unit-tested only).
7. Close the shared SSH masters at release instead of leaving them to
   `ControlPersist`.
8. Exposure: site forwarders publish API, gossip and probe (which now relays
   invocations) on every address of the site, i.e. its campus-facing address.
   Bind them to the address the operator and peers use and keep that list
   minimal; without sudo there is no firewall to add.

15. The VM resource sampler streams its samples over SSH only: if a stream
    drops, the run fails ("VM telemetry stopped"). Reopen the stream (the
    coverage accounting already records the gap) instead of failing.
16. Done after the third run: the relay also serves replica `/metrics` (210
    samples were refused with 403), and remote container checks record every
    outcome in `liveness.jsonl` and fail the run only after 5 minutes of
    continuous unreachability (a container reported stopped or missing still
    fails at once).

Maintainability:

9. Update the design spec: it still describes Loki per site and NSG rules
   rewritten after creation.
10. Remove the `getattr(..., default)` fallbacks added for test doubles
    (`node_distributions`, `shared_physical_host`, `request.lifecycle`) by
    updating the doubles; they hide missing attributes in production code.
11. Tests must not read this operator's paths (`PREBUILT = /datassd/...` in
    `test_multisite_resources.py`): generate the archives in the test.
12. A quiet baseline. Locally 49 tests fail before any change (Node.js
    absent, nanoFaaS checkout contracts against a non-CI revision), which hides
    regressions, and the suite takes ~28 min; provide Node.js to the test
    environment, pin the contract revision, and parallelise with xdist.
13. An operator watch command: a small script that follows a run's
    `supervision.jsonl` and prints state changes (step, measurement phase,
    failures, exit), instead of ad-hoc log greps.
14. Land the work: the `one-shot-multisite` branch (4 commits) and
    `fix-azure-min-disk` (Azure disk check) are local only; open PRs.

## Order and checkpoints

1 → 2 → 3 are pure refactors with regression tests (checkpoint: full suite
equals baseline). 4–6 need `.35` for integration (checkpoint: one forwarded VM
reachable, gossip between two VMs through forwarders). 7 needs an MFA login
(checkpoint: cloud VM up/down cleanly). 8–11 then 12.
