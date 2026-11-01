# B4: pressure offload pilot on DGX Spark and Azure

The pilot asks when pressure-based forwarding improves valid completions and
goodput at a fixed edge budget, and what full-response latency it adds. Compare
`disabled` and `pressure` at identical offered rates and inputs. The `cloud` arm
sends traffic directly to the terminal cloud and diagnoses its capacity; it is
not a third treatment in the primary edge comparison.

The NanoLab controller runs **on the DGX Spark**. Its workflow creates two local
Multipass VMs: the edge (4 vCPUs, 12 GiB RAM, 64 GiB disk) and the independent
load generator (2 vCPUs, 4 GiB RAM, 30 GiB disk). The same workflow creates the
terminal cloud as an Azure Spot `Standard_D8s_v5` VM with a 64 GiB disk, in
`maurinoRicerca-rg`, `westeurope`. Every arm gets new VM names and a new results
directory. Run arms sequentially so the DGX does not host overlapping experiments.
The controller may be reached over SSH; no Azure-initiated connection into the
DGX is required.

The cloud's NodePort rules allow the controller's public address, discovered
from its SSH connection to the cloud (`operator_source_cidr: auto`). Both
Multipass VMs must use that same public NAT address for cloud-bound traffic.
Their private LAN addresses are not Azure ingress sources. A VPN or policy route
that gives either VM different egress must be corrected before measurement.
The workflow checks the edge-to-cloud route before load and saves `cloud-route.json`;
k6 separately qualifies the generator-to-cloud path.
Do not broaden ingress to `0.0.0.0/0` to work around a failed probe. The WAN contribution is included in end-to-end response latency;
this protocol neither controls nor separately estimates network RTT.

## Prepare inputs without launching VMs

Use the NanoLab Python environment containing this change. Freeze the NanoLab
and nanoFaaS revisions before running the pilot, and retain both checkouts
alongside the manifests. Different image architectures are expected; source
fingerprints and the edge image identity must be checked across matched arms. From the NanoLab
checkout on the DGX, set the source checkout and controller storage explicitly:

```sh
export NANOFAAS_ROOT=/data/nanofaas
export NANOLAB_WORKSPACE=/data/nanolab-workspace
export TMPDIR=/data/tmp
mkdir -p "$NANOLAB_WORKSPACE" "$TMPDIR"
python scripts/b4-prepare-pilot.py --ssh-key /absolute/path/to/azure-key.pub
```

`--ssh-key` is required. It names an absolute **public-key path on the DGX**;
the matching private key must be usable there when the workflow runs. Preparation
does not read key contents, infer a local credential, authenticate to Azure, or
launch anything. Azure authentication and permission to create/delete the Spot
VM in the specified resource group are needed only for execution. The DGX also
needs the normal NanoLab host tools, including Multipass, Ansible, rsync and SSH.
VM bootstrap installs the remote runtime, Kubernetes and k6 tooling.

The script prints a unique directory under `/data/nanofaas-b4`; use
`--output-root /data/another-directory` to change the parent. It creates:

- `manifest.json`, with provisional rates, arm order, paths and commands;
- nine arm directories containing validated `scenario.yaml` and `environment.yaml`;
- `commands.sh`, containing ordinary `nanolab run` commands and stopping on the
  first failed command. It does not implement a provisioning engine.

There is one run per mode at 20, 100 and 300 requests/s. At successive rates, the
paired order is disabled/pressure, pressure/disabled, disabled/pressure. The
cloud diagnostic follows each pair. This small pilot is not a balanced replicated
study and its rate grid is **provisional**.

The environment pins the existing project Ubuntu image
`Canonical:ubuntu-24_04-lts:server:24.04.202607140`. Confirm that this exact image
and Spot VM size are offered in the region before executing. If availability
requires another image, update the preparation tool to a deliberately selected
exact version and generate new inputs for all arms; do not substitute `latest`. Record actual
image and host identities from the run evidence.

## Run and retain every attempt

Inspect the generated manifest and YAML files first. The emitted commands have
this form, with absolute paths chosen by preparation:

```sh
nanolab run /data/nanofaas-b4/pilot-.../01-disabled-r20/scenario.yaml \
  --environment /data/nanofaas-b4/pilot-.../01-disabled-r20/environment.yaml \
  --run-dir /data/nanofaas-b4/pilot-.../01-disabled-r20/results
```

Execute individual commands, or run `sh /data/nanofaas-b4/pilot-.../commands.sh`
after substituting the printed directory. These commands launch resources;
preparation alone does not. Do not add `--keep` or reuse a populated results
directory. The normal workflow owns bootstrap, builds on each architecture,
collection and cleanup. Builds have no additional CPU or memory caps; measurement
limits apply to deployed containers, not build processes. Builds remain bounded
by each VM's hardware allocation.

Every arm uses `word-stats-java`, the archived medium corpus and its exact body
oracle. The function limits are 2 CPUs / 512 MiB, with requests of 1 CPU / 256 MiB;
the control plane limits are 2 CPUs / 2048 MiB, with requests of 0.25 CPU / 512 MiB.
These budgets are the same in all modes and on both platforms. Edge concurrency
is 2 and queue capacity is 20; cloud concurrency is 20 and queue capacity is 100.
Admission is explicit `SYNC_QUEUE`, application retries are zero, and the terminal
cloud does not forward again. Each arm has a separate 60-second warmup and a
180-second measurement at its declared rate, with 512 generator VUs. Generator
capacity is a hypothesis to qualify, not proof that the offered work was emitted.

Keep partial and failed results, including Spot evictions and failed qualification.
The generated shell stops after a failed arm. Inspect that attempt, then prepare
a fresh directory and run the replacement arm with its new names and paths.
Keep the original arm's identity beside the replacement when comparing attempts;
never overwrite it or count only successful attempts. Controller evidence under
`/data` survives normal VM teardown; abrupt controller termination may still
require checking workflow-owned resource cleanup.

## Inspect evidence and recalibrate

Inspect `protocol.json`, `inputs/`, `observations/`, `k6-events.jsonl`,
`k6-summary.json`, `k6-result.json`, `execution.json`, control-plane logs and
`report.json` in each results directory. Missing files, missing counter samples,
malformed ledgers and failed collection are evidence failures, not zero events.
Keep the k6 exit status and thresholds even when request accounting reconciles.
An HTTP 200 is a valid completion only if its response matches the archived
oracle. Separate transport errors, refusals, other HTTP failures and invalid bodies.

Use scheduled requests and emitted requests as separate denominators. Goodput
is valid measurement-cohort completions (including the drain) divided by the
scheduled measurement duration, not wall-clock throughput. k6 can schedule one
extra boundary iteration per phase: the report retains the nominal count,
requires exact ledger/counter accounting, and uses the actual scheduled count
for completion probability. More than one extra iteration, missing ledger
records, or unexplained counts fail integrity verification. Compare
overall full-response p50/p95/p99 and the local/offload splits for valid responses, retaining
sample counts. The marker describes the observed proxy path; it does not establish
physical exactly-once execution. Cloud successes are already included in edge
successes and must not be added to them. Keep cloud-direct traffic outside the
primary comparison, along with warmup and setup work.

Before freezing a paper campaign, use the disabled pilot to locate baseline
capacity, inspect generator drops and resource pressure, and check the direct-cloud
diagnostic has the necessary headroom. Refine the provisional rates as needed,
then freeze one common grid, workload rotation, resources and acceptance rules.
Run at least three paired repetitions with alternating treatment order and fresh
attempts. A single successful pilot or the old hardware's throughput is not a
capacity estimate for this topology. This preparation tool intentionally emits
only the pilot; final calibrated replication is a separate protocol decision.

Reproduce the per-arm analysis without running VMs:

```sh
python -m nanolab.tasks.offload_experiment_report /data/nanofaas-b4/pilot-.../01-disabled-r20/results
```

This regenerates `report.json`, `report.md` and `measured-requests.csv` from the
archived evidence. A nonzero exit signals invalid or incomplete evidence; it
does not discard it. Raw Prometheus history is retained for resource diagnostics;
the current report makes no RAM estimate.
