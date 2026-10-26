# Experiment 02: complete platforms under one CPU/memory budget

`compare` measures complete FaaS deployments on Docker, on an Azure stack VM
without k3s. Each deployment runs inside one cgroup whose CPU quota and memory
limit are a budget from the protocol's grid. The calendar, the request ledger,
the receipts and the per-step verdicts are experiment 01's; what this experiment
adds is the budget, the Docker stack, the platforms and the cost per useful
response.

**Status: foundation implemented and verified locally; not yet run on Azure.**
This covers nanoFaaS P0 and P1 and the function alone. The baseline platforms
(tinyFaaS, Fn, Serverledge, faasd, FunLess, Sledge, Spin) are later work and
have no adapter yet. No result, capacity or budget follows from the local tests.

## Configurations

| Key | What runs | Built from |
| --- | --- | --- |
| `nanofaas-p1-jvm` | core, `async-queue`, Docker provider; full-tiering JVM | `experiment-02-p1-jvm.yaml` (publishes the function) |
| `nanofaas-p1-native` | the same modules, native -O3 | `experiment-02-p1-native.yaml` |
| `nanofaas-p0-jvm` | core only, in front of the function as an external endpoint; JVM | `experiment-02-p0-jvm.yaml` |
| `nanofaas-p0-native` | the same, native -O3 | `experiment-02-p0-native.yaml` |
| `direct-reference` | the function's own runtime, no platform | the function image |

Every nanoFaaS build includes `build-metadata`, uses Serial GC and holds the
same 64 MiB heap as experiment 01 (`-Xmx64m`, or `build.native.maxHeap: 64m`).
Every configuration runs the one `rust-qr-code` digest that
`experiment-02-p1-jvm.yaml` publishes. P1 registers it as a `DEPLOYMENT`
function with the protocol's fixed concurrency and queue capacity and
`maxRetries: 0`; P0 registers the function container as an `EXTERNAL`
endpoint. The function alone is sent `{"input": <payload>}` on its `/invoke`,
and answers with the PNG as a base64 JSON string; its container sets
`EXECUTION_ID`, which the runtime requires when no platform sends one.

The native recipes use the host builder. `validateRecipe` rejects them on macOS
("a native image needs a Linux host"), exactly like experiment 01's; they are
validated on Linux, in CI and in prepare on the stack VM.

## The stack and the budget

Provisioning installs Docker and the local registry, and no k3s. Right after
provisioning a new run, `compare` sets up three systemd slices on the stack VM:

- `nanolab-sut.slice` holds the deployment under measurement: dockerd and
  containerd (through drop-ins) and every container Docker creates (through
  `cgroup-parent` in `/etc/docker/daemon.json`), including those a platform
  creates on its own;
- `nanolab-infra.slice` holds what must not count: the registry, recreated
  there, and the sampler;
- both are children of `nanolab.slice`. Swap is turned off.

The setup runs once per run directory: on a resume it would recreate the
registry and discard the published images.

A cell applies its budget with
`systemctl set-property --runtime nanolab-sut.slice CPUQuota=… MemoryMax=… MemorySwapMax=0`,
restarts the engine inside it, and reads `cpu.max`, `memory.max` and
`memory.swap.max` back. A slice that is unbounded, may swap, or enforces another
budget stops the cell. So does any engine or container process found outside
the two slices before the load.

## Measurements

`assets/diagnostics/experiment02_cgroup_sampler.py` runs on the stack VM as a
transient unit in the infrastructure slice. Every 5 s it appends the raw
counters of the budget's cgroup and of its children: `memory.current`,
`inactive_file`, `usage_usec`, throttling and `oom_kill`. The series follow the
paper's definitions:

- memory is the working set, `memory.current` less `inactive_file`;
- CPU is `usage_usec` as a rate in cores over the 30 s before each sample;
- the engine's share is `docker.service` plus `containerd.service`;
- live instances are the number of `docker-*.scope` cgroups.

Per-response efficiency divides the CPU time and the memory occupancy (the time
integral of the working set) of each load step by the responses of its cohort
that were correct within the latency threshold. A step with no useful response
has no cost per response: `null`, not zero. A window the samples do not cover,
or one with a gap longer than two intervals, has no total either. The cost of a
step is read from the step's own window; a cohort's late responses may land in
the next step by at most `gracefulStopSeconds`, which is the known limit of
that attribution.

## A cell

1. Remove every container but the registry, or stop if one remains.
2. Apply the budget, restart the engine, read the budget back.
3. Start the sampler.
4. Pull any missing image, take the VM clock, start the platform, and wait for
   the API, the registration and the first correct response. Readiness is
   timed on the VM from that clock; a platform that gives no correct response
   before `readinessTimeoutSeconds` is `startup_failed`.
5. Stop if a process escaped the budget; record the containers and, for
   nanoFaaS, refuse a control plane whose modules or build type differ.
6. Run the calendar from the loadgen with `assets/k6/experiment-02.js`.
7. Always stop the sampler, fetch the samples and tear the platform down.

An OOM kill anywhere in the budget, before or during the load, makes the cell
`oom`. A refusal stops the matrix; any other failure before the load is retried
once in a new attempt; after the load started, it stops. Attempts, receipts and
resume work as in experiment 01, with the cgroup samples in place of the
Prometheus snapshot.

## The generator

`experiment-02.js` is experiment 01's calendar and ledger; only the target
differs per platform: the URL, the headers, and where the PNG is in the answer
(`nanofaas-envelope`, `json-base64` at a field or as the whole body, or
`raw-png`). The check is the same for every platform: a PNG signature and a
square IHDR at least the requested size and, once `pngSha256` is frozen, the
hash of the whole PNG. The pilot reads that hash from the first correct
response, recorded in each cell's `readiness.json`.

The loadgen reaches the stack on the NodePorts that provisioning already opens
to the operator and the loadgen only: every platform publishes its entry point
on 30080 (and nanoFaaS its management port on 30081). No other port is opened.

## Report

`experiment-02-summary.json` and `.csv` hold one row per cell and phase: the
configuration, the budget, experiment 01's counts, latencies and three verdicts,
the deployment's working set and CPU, the engine's share, the live instances,
CPU seconds and byte-seconds per useful response, and the readiness. Cells
without a receipt are listed as `incomplete`.

## Running it

```bash
export NANOFAAS_ROOT=/path/to/nanofaas   # a revision with Nanofaas/nanofaas#256
./nanolab.sh compare experiment-02-pilot.yaml \
  --environment /path/azure-comparison.yaml \
  --variants nanofaas-p1-jvm,direct-reference --repetitions 1 \
  --run-dir /path/runs/experiment-02-pilot
```

Without `--variants` all five configurations run; the native ones compile on
the stack VM, which is sized for that. The environment must be Azure with
exactly the `stack` and `loadgen` roles. As with every `compare`, the VMs are
kept after the run; remove them with the provider's teardown or list what is
left with `az resource list -g <group> --query "[?starts_with(name,'<name>')].id" -o tsv`.

## Local verification

- Protocol and scenario: `tests/config/test_experiment_02.py`.
- Matrix, identity, receipts, recipes and contract:
  `tests/comparison/test_experiment_02*.py`.
- Budget, sampler, generator script (through the Node harness), platforms and
  the cell: `tests/tasks/experiment_02/`.
- Docker-only provisioning and the Docker engine as target identity:
  `tests/test_provisioning.py`, `tests/comparison/test_target.py`.
- The attempt loop and `compare`'s refusals: `tests/cli/test_comparison_experiment_02.py`,
  `tests/cli/test_comparison.py -k experiment_02`.

Not established locally: systemd and Docker accepting the slice setup, the
sampler on a real cgroup, the readiness script against the real runtimes, the
native builds, and any measurement.
