# Heterogeneous one-shot Multipass campaign

Status: implementation and operator preparation. **No measured qualification or
main result is recorded yet.** This dossier is separate from the earlier
homogeneous CLI and logging technology pilots.

Five edges form the ring E1–E2–E3–E4–E5–E1 plus E2–E5; every edge also reaches a
terminal cloud. VM allocations are 1/1/2/2/3/8 vCPU and 2/3/3/4/6/12 GiB RAM.
Function CPU quotas are .35/.50/.60/.70/.85/1.0 with maximum warm replicas
1/1/2/2/2/7 and 256 MiB per replica. Scientific control planes have an explicit
.35 CPU / 512 MiB limit. Collectors have .1 CPU / 64 MiB, 256 MiB disk buffers;
owned Loki/Grafana have 1.5 CPU / 768 MiB combined. All fit the declared VM budgets.

One Rust input runs everywhere: 20,000,000 iterations, 4096 working-set bytes,
seed 7. Images, checksums, the frozen JVM control-plane recipe, graph, observer configuration and
NanoLab implementation fingerprint stay frozen. CPU quota emulates relative
edge speed on one shared physical ARM host; it does not establish geographic
latency or independence of successive blocks.

## Acquisition order

1. Run the full frozen-topology logging overhead gate (three paired runs;
   goodput loss and p95 increase each at most 5%). The old one-replica, P2P-off
   technology pilot cannot qualify this campaign.
2. Run `one-shot-heterogeneous-calibration.yaml` with real hash-bound
   runtime distribution. Measure each node independently; retain every sample.
   Sweep each local replica count at six load factors, three trials per factor
   (30s warmup + 60s measurement), plus three simultaneous trials. Initial sweep
   alone needs 6h45, before provisioning and other measurements.
3. Replace the qualification template's matrix with the nine complete six-node
   vectors from `capacity_trace(measured_capacities, settings, 5, seed)` (indices
   0,6,…,48). With q=.2 every candidate period 5/10/20/30/60 represents the same
   rates. Fifty complete samples per cell, q95, .05 period fraction, .2s auction
   and .3s ready margins are required; select T and lead from fresh observations.
4. `freeze_campaign_protocol(..., pilot_rows=[], ...)` and
   `write_campaign_scenarios(protocol, output)` emit six immutable standard
   `one-shot-experiment` YAMLs. Run them in registered order, each with a fresh
   six-VM topology for each mode. Verify actual peer and vertical SDK completions,
   physical saturation, healthy generation and cloud margin before main.
5. Freeze main with the six complete, registered pilot triplets. N is rounded to
   a multiple of six, minimum 12, maximum 36, using the declared 5pp/80% normal
   planning approximation and pilot paired standard deviation. If N exceeds 36,
   revise the budget explicitly rather than cap it. Pilot data never enter main.
6. Run every main YAML, retain all failures, then freeze the complete result
   registry and call `render_paired_campaign(..., protocol=protocol)`.

The bundled qualification and pilot files are **templates**, not qualified
inputs. Zero hashes and missing prerequisite paths must fail before any owned VM
is acquired. Actual commands, receipts, failure classifications and artifact
SHA-256 values belong in this dossier when acquisition runs. Operator image
archives and bulky HTML/JSONL live outside Git with immutable hash references.

Public commands, from the NanoLab checkout:

```sh
NANOFAAS_ROOT=<clean-runtime-checkout> ./nanolab.sh run <calibration.yaml> --environment <multipass.yaml> --run-dir <runs/calibration>
NANOFAAS_ROOT=<clean-runtime-checkout> ./nanolab.sh run <qualification.yaml> --environment <multipass.yaml> --run-dir <runs/qualification>
NANOFAAS_ROOT=<clean-runtime-checkout> ./nanolab.sh run <pilot/block-000.yaml> --environment <multipass.yaml> --run-dir <runs/pilot-000>
NANOFAAS_ROOT=<clean-runtime-checkout> ./nanolab.sh run <main/block-000.yaml> --environment <multipass.yaml> --run-dir <runs/main-000>
```

## Interpretation and retention

There are nine phases of six epochs: control, five seeded single-edge hotspots,
adjacent hotspots, diffuse overload, recovery. All six origins receive load.
Cloud-direct traffic is .05C and is separately accounted. Measured cloud and
physical r/D limits must both retain 10% margin even if all planned edge traffic
arrives there. Only the last four epochs per phase enter the primary denominator.

The primary contrast is EWMA minus baseline cloud completions divided by planned
edge originals. Negative values mean less vertical offload; errors cannot improve
the completion guard, whose lower paired 95% bound must be at least −2pp.
ORACLE is secondary, not an upper bound. Bootstrap resamples whole paired blocks
10,000 times with the frozen seed, never individual requests. Missing blocks or
algorithm-associated exclusions make inference inconclusive. Technical replacement
seeds/orders are preset, at most floor(.2N), and need an identified external cause
and retained original evidence; ambiguous failures cannot be selectively replaced.

Report correctness, routing coverage and effect size separately. A valid baseline
can have zero peer traffic, and a complete main with zero offload remains visible.
Busy handler samples are descriptive; actual sustained saturation/cloud margin
are separate pilot gates. Shared-host contention, time order and service drift
remain methodological limitations even after fresh VM provisioning. Intervals
are conditional on block independence; fresh VMs do not prove it.

For each mode at T=20s, 54T = 18 minutes of measured load, plus >=2T warmup and
provisioning/cleanup. Main has 3N modes (at least 10h48 of measured load at that T),
plus 18 pilot modes and calibration/qualification; actual T determines the final
wall-time estimate. Preserve node logs before VM deletion, all raw requests and
proofs, inventories, exact peers and failure artifacts. Restart the temporarily
stopped preexisting `nanofaas-stack` VM on every campaign exit path.
