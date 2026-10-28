# Experiment 02: complete platforms under one CPU/memory budget

`compare` measures complete FaaS deployments on Docker, on an Azure stack VM
without k3s. Each deployment runs inside one cgroup whose CPU quota and memory
limit are a budget from the protocol's grid. The calendar, the request ledger,
the receipts and the per-step verdicts are experiment 01's; what this experiment
adds is the budget, the Docker stack, the platforms and the cost per useful
response.

**Status: nanoFaaS P1 JVM, the function alone and five baselines (FunLess,
Spin, tinyFaaS, Serverledge, Fn) run on Azure, one cell each at 2 CPUs and
2048 MiB (27 October 2026).** faasd, on its own containerd VM, is the next
plan; Sledge is out of the comparison (it cannot compile the shared function,
see below). The pilot thresholds are not frozen, so no step is qualified and
no result, capacity or budget follows from these runs.

## Baselines

Every baseline runs the paper's one qr-code implementation,
`nanofaas-paper/functions/qr-code`, through a thin wrapper. The source is
taken at a commit (`NANOLAB_WORKLOADS_ROOT`, recorded in the identity), built
once per run on the stack in `rust:1.99-alpine` (pinned by digest), and
refused unless `qr-stdio` renders the measured PNG. Each platform is
installed under `/opt/nanolab-e02/<key>/<identity>`, with its own image tag,
where the identity hashes the pinned versions, the install script (the FunLess
compose file included), the workload tree the wrapper files come from and the
built artifacts' sha256: a failed step stops the install, and another run on
the same VM never changes what this one installed. The identities, digests
and versions an install resolves to are recorded in `prepare/baselines.json`.
A resume whose inputs make another identity stops before installing anything;
one that finds the digests or versions changed stops after.
Every cell first tears down whatever an earlier baseline left.

| Key | Platform | How it runs the function | Generator target |
| --- | --- | --- | --- |
| `funless` | FunLess 0.9.0 (core, worker, Postgres in compose; no Prometheus, no ELK) | Wasm module, FunLess's ABI | `POST /v1/fn/e02/qr`, PNG at `data` |
| `spin` | Spin 4.2.5 (`spin up`, checksum verified) | Wasm component (WASI preview 2) | `POST /` |
| `tinyfaas` | tinyFaaS `eff00d0` (manager on the host) | `binary` runtime, a process per request | `POST /qr` |
| `serverledge` | Serverledge `45419f5` with etcd 3.5.13 | `custom` image, stock Executor, a process per request | `POST /invoke/qr`, PNG in `Result` |
| `fn` | Fn 0.3.770 single-server | FDK contract over a Unix socket | `POST /t/e02/qr` (HTTP trigger) |

What runs on the host (Spin, tinyFaaS's manager and proxy, Serverledge) runs
in one systemd unit inside the measured slice; a process found outside stops
the cell. Concurrency knobs follow the protocol where a platform has one
(tinyFaaS `threads`, Serverledge `--max_concurrency`); Serverledge's container
pool gets the cell's budget. Otherwise the documented defaults hold, with the
platform's own services (FunLess's Postgres, Serverledge's etcd) inside the
budget. FunLess's guest token is read on the stack at each cell and reaches
only the generator, never the evidence.

Behaviour to declare with any result:

- **Fn creates and removes function containers with the load** (up to 109 in
  a cell at 200 requests/s), unlike the fixed replica of the P1 pairing. The
  pairing is kept and this is reported, as the spec asks.
- **A failed function answers HTTP 200** on tinyFaaS (`exit status 1`) and on
  Fn's trigger and `/invoke`, and **500 with a misleading message** on
  Serverledge; the generator judges the PNG, so such answers are incorrect.
- **tinyFaaS keeps CoAP and gRPC on their default ports**: its proxy at
  `eff00d0` exits with fewer than two protocols (`cmd/rproxy/main.go:22`).
- **tinyFaaS and Serverledge's stock Executor fork a process per request.**
- **Sledge is excluded**: aWsm does not compile the bulk-memory instructions of
  the WASI target's precompiled libc (`nanofaas-paper/notes/evidence.md`).

## First Azure runs

Baselines (`runs/experiment-02-baselines-smoke-3` and `-4`): every platform
reached its first correct PNG, sha256 `aa04cabc…`.

| Configuration | First correct PNG | Within threshold at 25 / 50 / 100 / 200 requests/s |
| --- | --- | --- |
| `direct-reference` (admission 2) | 1.6 s | 1501/1501, 2994/3001, 5617/6001, 2609/12001 (429s) |
| `spin` | 0.6 s | 1501/1501, 3000/3000, 6001/6001, 88/11620 |
| `serverledge` | 4.1 s | 1499/1501, 2989/3001, 4483/6001, 4240/11938 (500s) |
| `fn` | 5.9 s | 1501/1501, 3000/3000, 3596/5709, 808/8185 (timeouts) |
| `tinyfaas` | 4.8 s | 1501/1501, 3001/3001, 83/4533, 0/7705 (timeouts) |
| `funless` | 14.4 s | 1501/1501, 3001/3001, 62/4001, 0/7662 (timeouts) |

At the higher steps several generators sent fewer requests than scheduled;
with no frozen generator thresholds those steps are only `unverified`. A
resume of the finished run ran no cell and rebuilt nothing.

nanoFaaS and the function alone:

Pilot calendar, budget `cpu2-mem2048`, `Standard_D8s_v5` stack and
`Standard_D2s_v5` loadgen. The runs exercised the whole path: slices, budget
read-back, sampler with agents and scrapes, VM-side readiness, the shared
generator, per-step cleanup, receipts and the report.

- `nanofaas-p1-jvm` (`runs/experiment-02-smoke-3`): `measured`.
  - Readiness: API at 8.9 s, registered at 10.5 s, first correct PNG at
    10.7 s.
  - Warmup and step-01: all responses within the threshold, p99 about 30 ms,
    about 0.68 cores.
  - From step-02 on: 429s and p99 above 6 s.
  - Working set: from 634 MiB idle to 790 MiB. Agents: 12 MiB.
  - Scheduler scrapes: cover the calendar.
- `direct-reference` (`runs/experiment-02-smoke-2`): `measured`, with each
  request under its own execution id.
  - From step-01 on, the runtime saturates the 2-CPU quota and serves about
    4.5 correct responses per second. The rest are 429s.
  - CPU per correct response rises about thirtyfold over the warmup's, and the
    cgroup is heavily throttled.
  - The cause is under investigation (`notes/experiment-02-followup-prompt.md`
    in the paper repository); the row is not comparable until it is
    understood.
- The first smoke (`runs/experiment-02-foundation-smoke`) predates the review
  fixes and is not usable: it shared one execution id, and its P1 cell failed
  readiness on the registration retry.

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
endpoint. The P0 function container is given the control plane's
`CALLBACK_URL`, as the containers the Docker provider creates in P1 are: the
function behaves the same in both, at the price of one callback (answered 204)
per invocation in P0.

The function alone is sent `{"input": <payload>}` on its `/invoke` and answers
with the PNG as a base64 JSON string. The runtime admits one active execution
per id and answers 429 to a second (`sdks/rust/src/occupancy.rs`), so every
request carries its own `X-Execution-Id`, the request's ledger id. A first
Azure run, which shared one id through the container's `EXECUTION_ID`, lost
half of step-03 and two thirds of step-04 to 429s; that run is not usable.

The runtime's own admission is capped at the protocol's `fixedConcurrency`
(`NANOFAAS_MAX_CONCURRENT_HANDLERS`), the concurrency nanoFaaS P1 admits. With
its default of 32 the runtime fell into a contention state inside the
container, with variable onset and with or without the CPU quota; at a cap of
2 the first plateau stayed healthy in both diagnostic repetitions (paper
repository, `notes/evidence.md`, 27 October 2026). The cap is a comparison
policy, not a fix: the contention's cause is not identified, and the function
alone still has no queue in front of it.

The native recipes use the host builder. `validateRecipe` rejects them on macOS
("a native image needs a Linux host"), exactly like experiment 01's; they are
validated on Linux, in CI and in prepare on the stack VM.

## The stack and the budget

Provisioning installs Docker and the local registry, and no k3s. Right after
provisioning a new run, `compare` sets up the systemd slices on the stack VM:

- `nanolab-sut.slice` holds the deployment under measurement: dockerd and
  containerd (through drop-ins) and every container Docker creates (through
  `cgroup-parent` in `/etc/docker/daemon.json`), including those a platform
  creates on its own;
- `nanolab-infra.slice` holds what must not count: the registry, recreated
  there;
- `nanolab-infra-observers.slice`, inside it, holds the measurement agents, so
  their own cost is measured apart from the registry's;
- all are under `nanolab.slice`. Swap is turned off.

The setup runs once per run directory: on a resume it would recreate the
registry and discard the published images.

A cell applies its budget with
`systemctl set-property --runtime nanolab-sut.slice CPUQuota=… MemoryMax=… MemorySwapMax=0`,
restarts the engine inside it, and reads `cpu.max`, `memory.max` and
`memory.swap.max` back. A slice that is unbounded, may swap, or enforces another
budget stops the cell. So does any engine or container process found outside
the slices before the load.

## Measurements

`assets/diagnostics/experiment02_cgroup_sampler.py` runs on the stack VM as a
transient unit in the observers slice. Every 5 s it appends the raw counters of
the budget's cgroup, of its children and of the agents' own slice
(`@observers`): `memory.current`, `inactive_file`, `usage_usec`, throttling and
`oom_kill`. It writes its first sample before the cell goes on, and a last one
when it is stopped. The series follow the paper's definitions, read from the
cgroup directly rather than from cAdvisor:

- memory is the working set, `memory.current` less `inactive_file`;
- CPU is `usage_usec` as a rate in cores over the 30 s before each sample;
- the engine's share is `docker.service` plus `containerd.service`;
- the agents' cost is `@observers`, outside the budget, never added to or
  subtracted from the deployment's;
- live instances are the number of `docker-*.scope` cgroups.

For nanoFaaS the same process scrapes the control plane's Prometheus
exposition (`/actuator/prometheus` on 30081) at every tick and keeps it whole,
labels, counters and buckets included. A failed scrape is recorded as an
error; a series P0 does not expose stays absent.

Every value needs its window covered: samples on both sides of it, no gap
longer than two intervals, no counter going backwards. Otherwise the value is
unknown, with the reason recorded, never zero. This holds for means, maxima,
counts and deltas from idle as much as for totals; a CPU rate also needs its
whole 30 s look-back. The ledger's service values and verdicts stand whatever
the samples' coverage.

Per-response efficiency divides the deployment's CPU time and its memory
occupancy (the time integral of the working set) over a cohort's arrival window
and the drain after it, `[s, s + T + drainSeconds]`, by the responses of that
cohort that were correct within the latency threshold (paper §4.5). The
windows of neighbouring steps overlap: a step's value includes the concurrent
work of other cohorts, is not an exclusive attribution, and does not add up
across steps. The last step's window ends at the calendar's drain end. A step
with no useful response has no cost per response: `null`, not zero.

## A cell

1. Remove every container but the registry and any sampler left running, or
   stop if one remains. Record the slice's `memory.events` over SSH.
2. Start the sampler and wait for its first sample.
3. Apply the budget, restart the engine, read the budget back.
4. Pull any missing image, then start the platform from one script on the VM
   (`assets/diagnostics/experiment02_readiness.py`). It takes the clock, starts
   the containers and polls every 0.5 s for the API, the registration and the
   first correct response, so no SSH round trip lands inside a timing. A
   registration that outlives its client still counts once the function is
   found; an HTTP 200 with the wrong content does not end the wait. A platform
   with no correct response before `readinessTimeoutSeconds` is
   `startup_failed`.
5. Stop if a process escaped the budget; record the containers and, for
   nanoFaaS, refuse a control plane whose modules or build type differ.
6. Run the calendar from the loadgen with `assets/k6/experiment-01.js`.
7. Whatever happened, collect the evidence and clean up, each step on its own:
   `memory.events` again, the containers' state and logs, stopping the sampler,
   fetching the samples, the scrapes and the ledger, the teardown. A failed
   step is written to `diagnostics/cleanup-errors.json`.

An OOM kill in the budget, shown by the `memory.events` counter, the samples or
a container's `OOMKilled`, makes the cell `oom` whatever else happened, the
engine restart and a failed generator included. OOM evidence that cannot be
compared is unknown and stops the matrix. A refusal stops the matrix; any other
failure before the load is retried once in a new attempt, provided the cleanup
succeeded; after the load started, it stops. A non-zero k6 exit is never
`measured`. A failed cleanup keeps the receipt of a documented `oom` or
`startup_failed`, keeps no measurement, and stops the matrix. Attempts,
receipts and resume work as in experiment 01, with the cgroup samples, and for
nanoFaaS the scrapes, in place of the Prometheus snapshot.

## The generator

Experiment 02 runs experiment 01's own script, `experiment-01.js`, so the
calendar and the ledger are the same file in both experiments. An
`EXPERIMENT_TARGET` names what differs per platform: the URL, the headers, the
header that carries each request's id, and where the PNG is in the answer
(`nanofaas-envelope`, `json-base64` at a field or as the whole body, or
`raw-png`). Without it the script behaves as in experiment 01. The check is
the same for every platform: a PNG signature and a square IHDR at least the
requested size and, once `pngSha256` is frozen, the hash of the whole PNG. The
pilot reads that hash from the first correct response, recorded in each cell's
`readiness.json`.

The loadgen reaches the stack on the NodePorts that provisioning already opens
to the operator and the loadgen only: every platform publishes its entry point
on 30080 (and nanoFaaS its management port on 30081). No other port is opened.

## Report

`experiment-02-summary.json` and `.csv` hold one row per cell and phase: the
configuration, the budget, experiment 01's counts, latencies and three verdicts,
the deployment's working set and CPU with their coverage, the engine's share,
the agents' cost, the live instances, the efficiency window and the CPU seconds
and byte-seconds per useful response, the three readiness times and, for
nanoFaaS, whether the scrapes cover the measurement (`complete` or `partial`;
`not_applicable` for the function alone). Cells without a receipt are listed as
`incomplete`.

## Limits

- The engine is inside the budget and reported apart; so is a few MiB of the
  registry's own shim and port proxy, which run under containerd and dockerd.
- Inside the budget, a runtime sees all eight host CPUs: the slice's quota is
  not visible in a container's cgroup namespace, so a runtime that sizes its
  threads from the CPU count sizes them from the host.
- Phase windows come from the generator's clock and the samples from the
  stack's; a skew between the two VMs shifts the windows, as in experiment 01.
- Readiness is sampled every 0.5 s.

## Running it

```bash
export NANOFAAS_ROOT=/path/to/nanofaas   # a revision with Nanofaas/nanofaas#256
./nanolab.sh compare experiment-02-pilot.yaml \
  --environment /path/azure-comparison.yaml \
  --variants nanofaas-p1-jvm,direct-reference --repetitions 1 \
  --run-dir /path/runs/experiment-02-pilot
```

Without `--variants` the ten stack configurations run, which needs
`NANOLAB_WORKLOADS_ROOT` (the `functions/` directory of the paper repository); the native ones compile on
the stack VM, which is sized for that. The environment must be Azure with
exactly the `stack` and `loadgen` roles. As with every `compare`, the VMs are
kept after the run; remove them with the provider's teardown or list what is
left with `az resource list -g <group> --query "[?starts_with(name,'<name>')].id" -o tsv`.

## Local verification

- Protocol and scenario: `tests/config/test_experiment_02.py`.
- Matrix, identity, receipts, manifest, recipes, contract and summary:
  `tests/comparison/test_experiment_02*.py`.
- The shared generator, with and without a target:
  `tests/comparison/test_experiment_01_generator.py`,
  `tests/tasks/experiment_02/test_experiment_02_script.py`.
- Budget, sampler (run against fake cgroup trees and a fake exposition),
  readiness script (run against a fake platform), platforms and the cell:
  `tests/tasks/experiment_02/`.
- Docker-only provisioning and the Docker engine as target identity:
  `tests/test_provisioning.py`, `tests/comparison/test_target.py`.
- The attempt loop, `compare` end to end with fake VMs, and its refusals:
  `tests/cli/test_comparison_experiment_02.py`,
  `tests/cli/test_comparison.py -k experiment_02` (the end-to-end test needs
  `NANOFAAS_ROOT`).

Not established locally: systemd and Docker accepting the slice setup, the
sampler on a real cgroup, the readiness script against the real runtimes, the
native builds, and any measurement.

## faasd on a dedicated containerd VM

Select `--variants faasd` explicitly and declare exactly `stack`, `loadgen`, and
`faasd` Azure roles. The default remains the ten stack configurations so an
existing invocation does not acquire an additional VM. The faasd role shares
the stack's pinned image and SKU, uses a distinct VM name, and requires an
operator source CIDR. The function image is built on stack from the same
recorded shared workload and imported through skopeo into faasd's persistent
local registry. Function registration pulls from that registry.

The dedicated VM installs faasd 0.19.7, containerd 1.7.27 and CNI 0.9.1 from
verified releases. The native budget uses private cgroup and mount namespaces:
containerd's namespace root is its delegated service cgroup inside
`nanolab-sut.slice`, and the daemon moves into a child before enabling CPU and
memory controllers. Its service containers and function namespace remain
descendants of the same enforced budget. The engine-only series reads the
`containerd.service/daemon` child, excluding its sibling function/service
cgroups. Required faasd services, including its registry, remain inside the
full-deployment budget. Credentials are read on the VM into memory only.

Native containment was verified on an Azure Spot VM on 28 October 2026 at
2 CPU, 2 GiB and no swap, including both containerd namespaces. That was a
manual containment probe, not a QR correctness smoke or a campaign result.
The CLI integration still requires a fresh full Azure smoke with the shared
QR image and resume checks before experimental use.

The current provider configuration does not select Spot priority. When Spot
is requested, create the explicitly approved Spot VMs first and set `host` to each live
public IP for every role. This selects verified SSH access to existing Azure
targets, without taking OpenTofu lifecycle ownership. The live name/IP, SKU,
region, image and running state must match; a stopped VM is never started
implicitly. Configured NSG restrictions still apply. Follow the operator approval policy for all VM lifecycle actions.
