# Single-version memory soak

The `soak` workflow describes one NanoFaaS source snapshot and its control-plane
and SDK processes on the local container backend. Every application role selects
a source build by default. The shipped P24 preset uses JVM processes.
There is no revision comparison, baseline/candidate pair, automatic campaign
closure, or Kubernetes support in these presets.

**Readiness is incomplete.** These presets and model tests do not establish that
the complete build-to-report workflow is executable. Before a real run, integration
must demonstrate loader and policy-receipt persistence, build-aware Sonata routing,
immutable deployment, effective runtime preflight, prerequisite execution, live
collection/diagnostics, and full-run acceptance. The initial report implementation
is numerical-only and always records `p24_qualified: false`. An explicitly
approved real smoke of the integrated workflow remains necessary before P24.

The presets establish resource and protocol inputs. Full retained-state publisher
coverage is still an integration requirement: the shipped required metrics cover
process/cgroup memory and JVM heap where applicable, not every P24 ownership
population. Do not accept a memory-only policy as complete P24 qualification.

| Preset in `packages/nanolab/scenarios-v2/` | Purpose and boundary |
| --- | --- |
| `memory-soak-sync-container.yaml` | JVM control plane and Java SDK, Node SDK; operator criteria required |
| `memory-soak-smoke-container.yaml` | Short observation/artifact exercise with inline smoke criteria |
| `memory-soak-prerequisites-container.yaml` | Short prerequisite-adapter exercise; still smoke, never a qualifying P24 receipt |

The strict model currently has only `p24` and `smoke` purposes. A short dedicated
qualifying prerequisite run needs an explicit controller/receipt contract; this
preset does not invent a third purpose or bypass the rule rejecting smoke
receipts. Its declared coverage IDs are `sync`, `error-timeout-cancellation`,
`async-late-callback`, `idempotent-replay`, and `function-name-churn`. Unsupported
IDs must be reported as incomplete. The prerequisite implementation must agree on
these IDs before the preset is operational. P24 uses `prerequisites.mode: run`;
there is no exemption for absent coverage or a successful ordinary smoke.

## Operator criteria file

The P24 preset deliberately references an unshipped file:

```yaml
soakPolicyFile: ./memory-soak-policy.yaml
```

Relative paths resolve against the scenario file's directory, irrespective of the
working directory. An explicit absolute operator path is also accepted. There is
no environment-variable fallback. Missing files must stop `inspect`, `plan`, and
`run` before provisioning or builds. The inline `criteria: []` is intentionally
invalid until the loader resolves the operator file.

The policy must contain exactly these keys:

```yaml
schema: nanolab-soak-policy-v1
criteria: []
```

This shows the file structure only. An empty list is invalid, and no numerical
P24 acceptance policy is shipped. Populate the list with reviewed criteria before
using it. The loader replaces **only** `soak.criteria`; it cannot alter resources,
roles, images, retention, workload, diagnostics, or phase durations. Extra keys
and unsupported policy schema versions must be rejected.

The public test/loader boundary is
`nanolab.cli.soak.resolve_soak_policy(data: dict, scenario_path: Path) -> dict`,
followed by `ScenarioConfig.model_validate(resolved)`. The internal
`load_soak_policy(data, path)` returns `(resolved_dict, receipt_or_none)`. The
resolver removes `soakPolicyFile` before strict scenario validation. With inline
smoke criteria there is no external policy receipt.

The receipt contains `path`, `sha256`, `size_bytes`, and
`resolved_criteria_fingerprint`. The CLI loader attaches it as the private
`_soak_policy_receipt` attribute; workflow integration must persist it with the
run evidence. It is not an extra public ScenarioConfig field. Freeze and hash the
validated, resolved `SoakConfig` as well, so the measured policy includes the
actual criteria. Receipt persistence is a required integration gate, not an
assertion that attaching an attribute already writes durable provenance.

For each role, provide at least a `maximum` criterion for
`cgroup_memory_usage_bytes` in `steady`, bounded by that role's memory limit, and
an RSS residual criterion in natural `drain`. Criterion IDs must be unique. Each
criterion declares role, metric, exact `label_selector`, unit, operation, phase,
window/deadline, applicable threshold/tolerances, and rationale. Memory uses bytes.
Criteria can reference only metrics declared in that role's `required_metrics`.
To add verified retained-state metrics, edit the scenario's role declarations
explicitly; the criteria file cannot add them.

`return_to_reference` requires a drain deadline and both absolute-byte and relative
tolerances. The stricter tolerance applies to the natural final-window maximum
relative to the declared natural-baseline median. `growth_review` requires an
explicit pre-run threshold and review of suspicious residual growth.
`expected_zero` requires a drain deadline and no numerical threshold. Derive zero
and retention assertions from verified owner/publisher contracts; a guessed meter
name or missing series cannot establish zero.

A P24 policy needs a documented basis for its budgets and residual allowances
before the measured run. Neither the observed plateau nor a green smoke justifies
a new threshold. JVM attribution does not waive an RSS rule. Use independently
reviewed runtime-specific policies for the JVM and native presets; a common file
name does not imply a common justified budget. Keep each run's resolved copy and
receipt. Do not replace a failing run's policy and relabel the result as passing.

## Schedule, workload, and resource costs

| Phase | P24 seconds | Smoke seconds |
| --- | --- | --- |
| Warm-up | 120 | 5 |
| Baseline drain | 2100 | 5 |
| Natural baseline window | 120 | 5 |
| Constant steady load | 5400 | 30 |
| Natural final drain | 2100 | 15 |
| Declared cleanup margin | 300 | 1 |

The P24 steady phase spans three cycles of the longest 1800-second window. Drain
covers that window plus a 300-second margin. Warm-up, baseline preparation,
diagnostics, builds, and teardown do not consume the 5400 seconds. Budget more
than 125 minutes for the full run; the 125 minutes cover steady plus final drain
alone. The short smoke is deliberately too short to qualify these retention rules.

Diagnostics that run between two measured phases do move what follows them: the
baseline checkpoint's captures take their own time (bounded by `diagnostics.timeout_s`)
before `steady` starts, so a run's wall clock grows by that capture and its phases
carry an extra recorded window. Nothing in the measured windows absorbs it.

The intended owner settings correspond to NanoFaaS's documented `sync-ttl: 30s`
(unkeyed synchronous outcomes), `ttl: 5m` (terminal keys/readable outcomes), and
`max-lifetime: 30m` (live key/execution ceiling). Their mapping in `retention_s`
does not configure NanoFaaS. Preflight must observe authoritative effective
values and bind them to these owners; an environment-variable declaration alone
is insufficient. Larger effective windows require a reviewed longer schedule.

Each preset explicitly caps the control plane at 2 CPU/1024 MiB, Java at
2 CPU/1024 MiB, and JavaScript at 1 CPU/512 MiB. These are chosen allocation caps,
not proof of workload capacity or acceptable retained memory. Allow up to 5 CPU
and 2560 MiB for these three application containers, plus the registry, generator,
observer, host, diagnostic helpers, and build tools. Native compilation has its
own potentially substantial resource cost outside the measurement interval.

P24 declares 100 requests/second per function (200 total) and 200 VUs. These are explicit
initial workload inputs, not a measured saturation claim. Validate achieved
offered work, correctness, errors, dropped iterations, and generator capacity.
The smoke offers 1 request/second per function with 4 preallocated/8 maximum VUs.
Its cgroup ceiling is its declared container limit; its RSS growth threshold is
zero. Growth therefore requests review instead of silently granting a large
residual allowance. A smoke may fail or be inconclusive; neither outcome licenses
relaxing P24 policy. All smoke reports must state `p24_qualified: false`.

P24 allows 8 GiB of evidence and a 3 GiB aggregate dump budget with at most three
dumps and a 120-second diagnostic timeout. Free disk must satisfy the configured
budget before workload starts. Smoke allows 16 MiB and no diagnostic dumps.
Sampling is every 10 seconds for P24 and every second for smoke. Missing samples,
timeouts, restarts, and identity changes remain visible; they never reset a series
or become zero-valued observations.

## Builds and diagnostic access

One stable, fingerprinted source snapshot supplies the control-plane, Java SDK,
and JavaScript SDK builds, including identified local modifications. The shipped
P24 preset selects `jvm` for both Java roles and `default` for JavaScript on
`linux/amd64`; the generic image builder continues to support native recipes.
The control plane explicitly selects
`container-deployment-provider` and `async-queue`. Changing modules, platform,
runtime options, payload, or source invalidates relevant evidence identities.

The required order is source/build preflight, snapshot, all role builds,
publication under run-owned tags, digest freeze, deployment, effective runtime
preflight, prerequisites and measurement. Record effective toolchains/base images,
recipe identities, commands, logs, and output digests. There must be no application
build after digest freeze and no fallback to an old tag after a failed build.
Explicit `prebuilt` mode requires immutable digests and provenance; it is not used
by these presets. A native Java recipe must never become a JVM build implicitly.

RSS/PSS from procfs, raw cgroup usage, heap used/committed, and derived working-set
estimates are distinct quantities. Do not add them. Keep heap/non-heap and pool
labels intact. Prometheus series cardinality and actual registry meter population
are separate observations. Full P24 coverage also requires owner-bound queues,
executions, outcomes, keys, pending callbacks/timers, threads/buffers/pools, retired
owners, and retained payload evidence as applicable to each role. Required absent
instrumentation blocks qualification even when HTTP succeeds and RSS is flat.

Local Linux procfs access must identify the actual measured process, and the
collector needs its configured Docker Engine socket and per-role metric endpoints.
Distroless images may lack diagnostic tools. JVM capture requires compatible
`jcmd`, verified attach/namespace and output-path access, and bounded helper
execution. Node capture requires private, verified inspector control. Provision
and identify helpers explicitly; no placeholder helper digest is shipped.
The helper is built at the start of each run from
`assets/soak/diagnostic-helper.Dockerfile`, published to the registry
preparation already uses, and pinned to the digest that build reported. No
scenario carries one: a digest names bytes in whichever registry built them, so
it is unpullable elsewhere and a prune breaks it even locally. The inputs stay
pinned in `assets/soak/mat.lock.json` and `assets/soak/helper-bases.lock.json`.
A scenario that still sets `helper_images`, or an injected
`RuntimeOptions.memory_helper_image`, is honoured as-is and skips the build.

P24 requests `gc` and `heap_dump` for all roles, with runtime-specific full-GC
event evidence. Those source identifiers are required integration bindings,
not assertions that a transport already publishes them. A successful command or
an unspecified GC counter increase is not proof of a completed matching full GC.
Empty `runtime_options` declares no additional options; effective settings still
need verification. If deployment adds options, declare and verify the actual
ordered values instead of silently accepting a mismatch.

Two checkpoints declare diagnostics: `operations` for the final drain, and
`baseline_operations` for the close of the baseline window. Declaring one reading
in both is how a difference is expressed, and the two share one run-wide
reservation, so `max_dumps`/`max_dump_bytes` are ceilings over both. Operations
run **in the order declared**, which is meaning rather than style for a reading
that measures a difference: a `native_memory_diff` declared after `gc` would
include that GC's own reclamation in its delta. Declare `gc` last.

Native Memory Tracking is captured that way, in the two spikes under
`scenarios-v2/memory-soak-p24-*-spike-container.yaml`: `native_memory_baseline`
at the baseline checkpoint, then `native_memory_diff` at drain for what grew per
category over steady and drain, and `native_memory` for the absolute reading.
They exist because NMT needs `-XX:NativeMemoryTracking` and costs a few percent,
so it is a diagnostic instrument and not part of a qualifying run.

Capture the natural final window before any forced GC or dump, and the baseline
window before the measured phases it is the reference for. Record diagnostic
perturbation intervals and completion evidence. Dump/root analysis must identify
owner, population, lifetime, policy budget, reviewer, and hashed artifacts.
Histograms alone do not establish ownership, and clean heap does not excuse
unattributed RSS. Missing helpers, full-GC evidence, or attribution remain explicit
gates, not exceptions that can be removed to get PASS.

## Commands after integration

Run from the NanoLab worktree, with a suitable operator policy in the location
referenced by the chosen P24 scenario. These are the intended public commands;
this documentation change did not render a plan, build images, or run containers.
Do not execute the run commands until the integration and real-run prerequisites
above are satisfied and the resource/diagnostic costs have been approved.

```bash
export NANOFAAS_ROOT=/home/michele/Documenti/nanofaas
./nanolab.sh inspect packages/nanolab/scenarios-v2/memory-soak-sync-candidate-diagnostic-container.yaml
./nanolab.sh plan packages/nanolab/scenarios-v2/memory-soak-sync-candidate-diagnostic-container.yaml
./nanolab.sh run packages/nanolab/scenarios-v2/memory-soak-sync-candidate-diagnostic-container.yaml
```

For the separately authorized integrated smoke:

```bash
./nanolab.sh inspect packages/nanolab/scenarios-v2/memory-soak-smoke-container.yaml
./nanolab.sh plan packages/nanolab/scenarios-v2/memory-soak-smoke-container.yaml
./nanolab.sh run packages/nanolab/scenarios-v2/memory-soak-smoke-container.yaml
```

`inspect` must show the resolved policy, and `plan` must show all application build
recipes without executing them. A parseable scenario or non-executing plan is not
runtime preflight evidence. Do not render the new workflow until its builder is
available. Prerequisite exercises use the similarly named prerequisites preset
only once the adapter routing exists; do not advertise its smoke as P24 coverage.

## Cancellation, evidence, and interpretation

Cancellation stops the generator, bounds final collection, flushes partial
evidence, records `ABORTED`, and releases owned resources. It must not wait through
the normal 35-minute drain. Each new attempt gets a new run identifier; partial
selection or resume cannot be presented as a continuous completed soak.

The existing explicit `--keep` mechanism, once connected to this workflow, keeps
the run's environment for investigation. It does not turn an interrupted run into
a valid one. Teardown must release only owned resources and preserve evidence;
it must not prune shared registries/caches or delete another run's containers.
Verify the integrated CLI's supported teardown invocation before using it.

Retain the resolved configuration and policy receipt, source/build identities,
effective preflight, prerequisite receipts, target identities, phase/events and
raw samples, workload results, logs, diagnostic receipts, artifact checksums,
and versioned JSON/Markdown reports. Offline evaluation must read these artifacts
without contacting the old containers. A new evaluation preserves the preceding
verdict and identifies any added attribution. The offline CLI owner supplies its
final command syntax; this guide does not invent an unintegrated subcommand.

`PASS` requires complete valid evidence and every mandatory criterion satisfied.
`FAIL` records demonstrated violations. `INCONCLUSIVE` records missing or invalid
evidence and unresolved required attribution. `ABORTED` records external
interruption while preserving already demonstrated per-criterion failures.
Non-PASS must return a nonzero process exit status. A numerical-only report cannot
establish full-run acceptance. See the [illustrative report](soak-example-report.md).

## Migration from legacy loadtest

The two historical preset paths now select `workflow: soak`. Remove
`loadProfile: mixed`, `soakMinutes`, `drainMinutes`, `asyncShare`, `idemShare`,
`loadScale`, `loadVus`, comparison image tags, and `controlPlaneRuntime` from a new
soak scenario. The strict soak workflow rejects these legacy inputs. Use explicit
`soak.phases`, `workload.rates`, per-role limits, and role-specific build recipes.
SYNC without idempotency keys is the dedicated workload contract, not legacy
mixed-load shares hidden in a new preset.

Other legacy loadtest scenarios retain their existing behavior. They do not
implement the new P24 evidence gate. The old native preset reached comparison
routing with incomplete prebuilt image inputs; the intended fix is dedicated soak
routing with native builds for both Java roles. These preset changes alone do not
prove the old plan regression fixed: that requires the integrated loader/builder
tests, without an actual image build during test execution.
