# Synchronized multi-function one-shot Multipass campaign

Status: software implementation and operator preparation. **No scientific VM
qualification or main result is recorded yet.** Earlier single-function artifacts
remain historical; their capacities and timing do not qualify this scope.

Five edges form two planar triangles E1–E2–E3–E1 and E3–E4–E5–E3, sharing E3.
Degrees are 2/2/4/2/2. Every edge also reaches a terminal cloud; peer offload is
one hop and cloud is terminal. All six nodes run concurrently.

| Node | vCPU | VM RAM GiB | Function pool MiB | CPU per replica | Individual maximum |
|---|---:|---:|---:|---:|---:|
| E1 | 1 | 2 | 320 | .100 | 5 |
| E2 | 1 | 3 | 320 | .100 | 5 |
| E3 | 2 | 3 | 640 | .149 | 10 |
| E4 | 2 | 4 | 640 | .149 | 10 |
| E5 | 3 | 6 | 960 | .166 | 15 |
| Cloud | 8 | 12 | 2240 | .214 | 35 |

These are candidate resources, pending final-quota/co-location verification.
Five distinct Rust functions run everywhere: arithmetic, sorting, memory scan,
integer matrix multiplication and a mixture of all four. Each has its own input,
independent expected checksum and canonical identity. The preliminary owned-host
memory gate selected 64 MiB for all five, with 50 physically released correct
samples each, peaks below 48 MiB and swap disabled. This is not a VM D profile.
Docker Memory=MemorySwap=M gives a hard RAM cap and no swap, and the native planner
charges the full cap. Individual maxima cannot all be allocated simultaneously.
Balanced initial vectors are 1/1/2/2/3/7 replicas per function by node.

Each VM reserves .35 CPU / 512 MiB for its JVM control plane, .1 CPU / 64 MiB for
Fluent Bit, and .05 CPU / 128 MiB for OS accounting. The 20-CPU operator host
budgets 17 VM vCPU, 1.5 CPU / 768 MiB for owned Loki/Grafana and one CPU for
operation. JSON logging is on for scientific calibration, pilot and main.

## Frozen load and analysis

Nine windows each contain exactly one 360-second round: control, five seeded
single-node hotspots, hotspots on E2/E4, diffuse overload on E1/E3/E5, recovery.
Each window contains all 30 positive node/function flows. Offered rates differ by
function, node and time and come from complete measured balanced-vector records.
The load transition and native planner share the same boundary. ORACLE gets the
trace; EWMA gets no future entries. Cloud-direct traffic stays low and separate.

Each mode measures **54 minutes across all six nodes**, plus at least 12 minutes
of warmup. Baseline, EWMA and ORACLE run separately with identical original
schedules. Version 2 protocols freeze five biunivocal aliases and
`analysisEpochIndices=[0,1,2,3,4,5,6,7,8]`. Every planned edge original enters the
primary denominator; cloud-direct originals are excluded. Only independent SDK
terminal RELEASED proofs establish completions or peer/cloud destinations.
Legacy version 1 remains last-four-of-six and is not reinterpreted.

The primary contrast is EWMA minus baseline cloud completions per planned edge
original. Its completion guard requires a lower paired 95% bound of at least
−2 percentage points. ORACLE is secondary, not an upper bound. Bootstrap resamples
whole paired blocks 10,000 times. Reports retain all cell outcomes and summaries
by function, node and round, observed graph, destination matrices, resource
limits, timings and raw evidence. The nine changing conditions support no
steady-state claim. Intervals are conditional on independent blocks; fresh VMs
and a shared host do not establish independence.

## Acquisition order and budget

1. Fresh clean runtime images and source/helper/hash inventories, full software
   gates, then three paired logging off/on bootstrap trials with five functions
   and native control. Both goodput loss and p95 increase must be at most 5%.
2. Final strict cgroups, memory/co-location, 30 local D profiles with at least 50
   complete samples per pair, then concurrent capacity sweeps. Every integer
   replica level, six factors, three repetitions, six different mixed vectors
   and additional simultaneous trials remain in raw evidence. A scalar capacity
   cannot qualify five functions.
3. Derive the nine candidate matrices, then three trials for each worst-case
   aggregate cloud mix using its fixed balanced vector. Every function must have
   at least 10% headroom in both measured and physical bounds. Unknown mixes
   fail before VM timing qualification.
4. Qualify T=360 only, all nine cells with at least 50 complete samples, q95,
   epsilon<=.05 and .2/.3-second auction/ready margins. SDK readiness and strict
   cgroups for all 35 cloud replicas precede sampling. Repeat three observer
   pairs on the qualified trace before accepting pilot.
5. Freeze and run six balanced pilot triplets through standard public scenarios,
   fresh six-VM topology per mode. Actual peer and vertical completions,
   saturation, generator health and cloud margin are gates. Missing coverage
   preserves data and triggers an explicit revision before main.
6. Freeze N from those six complete registered triplets: multiple of six,
   minimum 12, maximum 36, declared 5pp/80% paired planning approximation.
   Pilot never enters main. Run all main blocks and retain every failure.

Capacity requires 3,276 base trials plus 27 cloud-mix trials, each 90 seconds:
**at least 82h34m30**, before provisioning, transitions and evidence collection.
Timing requires 450 samples, each at least 363 seconds: **at least 45h22m30**.
The pilot alone takes 19h48 including minimum warmup; main at N=12 takes 39h36.
These repeated acquisitions are distinct from the 54 measured minutes per mode.
The future 60-second stage requires a separate decision and fresh qualification.

`one-shot-multifunction-{calibration,qualification,pilot}.yaml` are packaged
**templates** with intentional zero-hash blockers and provisional qualification
rates. Replace them only with real hash-bound prerequisites. The existing
`freeze_campaign_protocol` and `write_campaign_scenarios` emit immutable standard
`one-shot-experiment` YAMLs; no separate infrastructure orchestrator is needed.

```sh
NANOFAAS_ROOT=<clean-runtime-checkout> ./nanolab.sh run <calibration.yaml> --environment <multipass.yaml> --run-dir <runs/calibration>
NANOFAAS_ROOT=<clean-runtime-checkout> ./nanolab.sh run <qualification.yaml> --environment <multipass.yaml> --run-dir <runs/qualification>
NANOFAAS_ROOT=<clean-runtime-checkout> ./nanolab.sh run <pilot/block-000.yaml> --environment <multipass.yaml> --run-dir <runs/pilot-000>
NANOFAAS_ROOT=<clean-runtime-checkout> ./nanolab.sh run <main/block-000.yaml> --environment <multipass.yaml> --run-dir <runs/main-000>
```

Registry incompleteness or algorithm-associated invalidity makes inference
inconclusive. Technical replacements are preset, at most floor(.2N), require an
identified external cause and retain the original failure; ambiguity is not an
exclusion rule. Never retry originals. Preserve every failed gate, SDK proof,
request, raw log, ownership journal and cleanup error. Bulky artifacts remain in
`/tmp/oneshot-multifunction-20261010` with immutable references in this dossier.

The preexisting `nanofaas-stack` was permanently deleted with explicit user
approval. Its historical deletion receipt remains in the earlier operator root;
never recreate it. Preserve every other foreign resource. No campaign push or
merge is authorized.

The revised campaign additionally requires continuous whole-VM and container
telemetry at 1Hz. Its implementation and sampler hash are frozen before VM
measurements. Raw JSONL streams cover warmup and all nine epochs on each edge
and cloud, are retained on failures, and are included in coverage checks and
per-node temporal plots. A separate three-pair physical telemetry overhead gate
with logging enabled on both sides is required before scientific provisioning;
the local operator-host smoke is only a technology check. No VM saturation claim
is supported by control-plane process metrics or handler occupancy alone.

Starting telemetry before each capacity load reserves an additional 45-second
lead. For 3,303 capacity trials this adds at least 41h17m15 to the unchanged
82h34m30 of scheduled load: **123h51m45 before SDK preparation, VM provisioning,
collection and cleanup**. The 54-minute measured campaign window stays unchanged.
