# Single-version memory soak

The `soak` workflow describes one NanoFaaS source snapshot and its control-plane
and SDK processes on the local container backend. Every application role selects
a source build by default. The shipped P24 preset uses JVM processes.
There is no revision comparison, baseline/candidate pair, automatic campaign
closure, or Kubernetes support in these presets.

**P24 readiness remains incomplete.** The ARM64 recipe container smoke now
exercises the complete local build-to-report path. Its verification below
distinguishes evidence completeness from unresolved numerical RSS findings.
Reports retain `p24_qualified: false`; a short smoke does not qualify P24.

The presets establish resource and protocol inputs. Full retained-state publisher
coverage is still an integration requirement: the shipped required metrics cover
process/cgroup memory and JVM heap where applicable, not every P24 ownership
population. Do not accept a memory-only policy as complete P24 qualification.

| Preset in `packages/nanolab/scenarios-v2/` | Purpose and boundary |
| --- | --- |
| `memory-soak-sync-container.yaml` | ARM64 JVM/Node recipe preparation; shipped policy, full P24 readiness pending |
| `memory-soak-smoke-container.yaml` | Short observation/artifact exercise with inline smoke criteria |
| `memory-soak-smoke-recipe-container.yaml` | ARM64 JVM/Node sibling using one attested recipe publication |
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

The canonical P24 preset references the shipped six-criterion policy:

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

This shows the file structure only; an empty list is invalid. The shipped
`memory-soak-policy.yaml` contains cgroup ceilings and zero-threshold RSS
residual criteria for all three roles. These existing limits remain unchanged;
they do not establish complete retained-state acceptance. The loader replaces
**only** `soak.criteria`; it cannot alter resources,
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
The registry is acquired by the run under the name
`nanofaas-e2e-registry` and published on `0.0.0.0:5000`; a registry the run
created is removed when it ends, together with the anonymous volume its layers
went into, so nothing from a run survives it. If you keep a registry of your own
running, stop it before a run — a different container already bound to that port
makes the run's own creation fail rather than adopt yours.

The canonical P24 preset declares 20 requests/second per function (40 total)
and 200 preallocated/maximum VUs. These are explicit
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
`linux/arm64`; the generic image builder continues to support native recipes.
The control plane explicitly selects
`container-deployment-provider`, `async-queue` and `build-metadata`. Changing
modules, platform, runtime options, payload, or source invalidates relevant
evidence identities.

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
`assets/soak/diagnostic-helper.Dockerfile`, published to the run's own registry
— acquired before the run and, if the run started it, removed again when it ends
— and pinned to the digest that build reported. No
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

Run from the NanoLab worktree. The canonical P24 preset resolves its shipped
policy and recipe relative to the scenario file:
Do not execute the run commands until the integration and real-run prerequisites
above are satisfied and the resource/diagnostic costs have been approved.

```bash
export NANOFAAS_ROOT=/home/michele/Documenti/nanofaas
./nanolab.sh inspect packages/nanolab/scenarios-v2/memory-soak-sync-container.yaml
./nanolab.sh plan packages/nanolab/scenarios-v2/memory-soak-sync-container.yaml
./nanolab.sh run packages/nanolab/scenarios-v2/memory-soak-sync-container.yaml
```

For the separately authorized integrated smoke:

```bash
./nanolab.sh inspect packages/nanolab/scenarios-v2/memory-soak-smoke-container.yaml
./nanolab.sh plan packages/nanolab/scenarios-v2/memory-soak-smoke-container.yaml
./nanolab.sh run packages/nanolab/scenarios-v2/memory-soak-smoke-container.yaml
```

`inspect` must show the resolved policy, and `plan` must show all application build
recipes without executing them. A parseable scenario or non-executing plan is not
runtime preflight evidence. `plan` also shows the registry and builder this run
acquires and releases; a builder already present is adopted and left running.
Prerequisite exercises use the similarly named prerequisites preset
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
verdict and identifies any added attribution. Use
`./nanolab.sh soak-evaluate /path/to/saved/run/evidence` to reassess
saved evidence without publication or deployment. All resume and partial
measurement selections remain unsupported, and a new run requires an unused
run directory.

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

## Recipe preparation for container smoke

The reusable profile is
[`soak-container-smoke-jvm.yaml`](../packages/nanolab/recipes/soak-container-smoke-jvm.yaml).
Run its
[scenario](../packages/nanolab/scenarios-v2/memory-soak-smoke-recipe-container.yaml)
from the repository root:

```bash
NANOFAAS_ROOT=/path/to/pinned/nanofaas ./nanolab.sh run \
  packages/nanolab/scenarios-v2/memory-soak-smoke-recipe-container.yaml \
  --environment packages/nanolab/environments/local.yaml \
  --run-dir /tmp/nanolab-soak-recipe-run
```

This supported selection uses Linux ARM64 Docker on the host, JVM control
plane, Java JVM word-stats and JavaScript word-stats. The control plane
selects `container-deployment-provider`, `async-queue` and `build-metadata`.
Profile/platform/module/role contradictions fail before infrastructure
acquisition. The original smoke scenario remains available.

One captured source inventory supplies every application image. Disposable
Git staging preserves dirty, deleted, untracked and executable inputs;
`source-identity.json` distinguishes the original revision/fingerprint from
the staging commit. One root `publishRecipe` assembles and publishes all roles.
An explicit owned builder uses BuildKit v0.27.1 by image digest and a local
HTTP registry; its creation does not change global builder selection.

Evidence under `evidence/builds/` includes:

- `recipe/recipe.yaml`, `recipe/source-identity.json` and
  `recipe/distribution/distribution.json`;
- observer requests, instrumentation hashes, successful command logs and
  preserved Buildx metadata under `recipe/observer/`;
- raw index, executable manifest, configuration and bound maximum provenance
  statement under `registry/<role>/`;
- `build-<index>.json` receipts in configured role order and
  `runtime-images.json`, hashed into every receipt.

Compiler versions come from the actual successful build context. The plugin's
Node compilation log is paired with its cached push only when compilation
inputs match. Quoted JVM launcher files emitted by recipes are read through
the existing bounded, ownership-checked observer; unsupported quoting and
indirect option files remain unavailable observations.

The runtime deploys executable manifest digests and verifies image configuration
IDs, CPU/memory limits and effective launch options. Offline evaluation rechecks
the captured recipe/report, raw attestations and compiler observations. Report
recipe descriptors use `bake: null`; the legacy bake verifier keeps its
existing path. Empty declared diagnostic operations still produce a bound
empty `diagnostics.json` receipt.

### Local verification

The complete recipe smoke was executed across 30 September–1 October 2026
against NanoFaaS `e7914be065e844776af57fe9e449bce7f12e03c5`. The clean source
snapshot fingerprint is
`25eea60e44010ed346888db88c022a93864fa610f14a96cc8d059c5147377baf`.
Observed build tools: Java **25.0.4**, Gradle **9.7.1**, Node **20.20.2**,
BuildKit **v0.27.1**. One application publication produced three verified
receipts and distinct publication/executable/configuration identities.

All configured phases completed: 12-second warmup, 12-second baseline drain,
30-second baseline, 60-second steady and 45-second natural drain. Steady
work completed **122 requests**, with **zero HTTP errors**, **zero dropped
iterations** and **244/244 checks passed**. This smoke explicitly declares
no prerequisite profiles or diagnostic operations; both bound receipts exist.

The final report is **INCONCLUSIVE** (CLI exit **2**), with
`purpose: smoke`, `p24_qualified: false`. Source/artifact provenance, frozen
policy, continuity, effective preflight, workload correctness/accounting,
required observations, diagnostic coverage and artifact integrity all pass.
All three cgroup ceilings pass. Each zero-threshold RSS growth review remains
inconclusive and requires ownership/equal-work attribution; the attribution
and overall run-coverage gates therefore remain inconclusive. Thresholds
were preserved.

Evidence after final review fixes:
`/tmp/nanolab-soak-recipes-e2e-20260930/run-8/`; CLI log and
`verification-after-review.json` are in its parent directory. The report is under
`evidence/evaluations/evaluation-dd188f51e3ca4057a34a33d7f1f76c23/`.
Containers, volumes and the selected `nanolab-heap-analysis` builder matched
the pre-run state after cleanup. The ordinary JVM recipe lifecycle regression
also completed all 13 tasks successfully under `jvm-regression/`.

The full Bandit gate retains four preexisting low-severity B101 findings in
comparison/product/loadtest/validate assertions. New recipe/soak source files
have no Bandit findings. This is recorded separately from functional readiness.


The final review's three Important findings were reproduced and corrected:
unexpected source inputs are rejected, local and published provenance invocation
IDs are cross-checked online and offline, and serialized evidence producers share
pre-write quota accounting. The final full NanoLab suite passed **3,145 tests**;
Ruff, formatting, types and import contracts passed. Execution rulings and their
costs are recorded in the [implementation plan](superpowers/plans/2026-09-30-soak-recipes.md).

## Recipe preparation for the canonical P24 preset

[`soak-container-p24-jvm.yaml`](../packages/nanolab/recipes/soak-container-p24-jvm.yaml)
is selected by `memory-soak-sync-container.yaml`. It uses the same snapshot,
publication, registry verification and frozen-receipt path as recipe smoke.
The full resolved preset matches its pre-migration configuration except for
`recipeProfile`: phases, policy, workload, resources, diagnostics and prerequisite
coverage are preserved.

The profile explicitly carries the legacy control-plane Serial GC/C1 settings
(`-XX:+UseSerialGC`, `-XX:TieredStopAtLevel=1`). The Java function retains JVM
defaults: its legacy Dockerfile ignored the supplied `JVM_TUNING` build argument.
Changing or omitting the control-plane tuning, adding function JVM arguments or
introducing other profile build overrides fails P24 validation. Smoke retains
its separate launcher policy.

### P24 preparation verification

On 2 October 2026, native Linux ARM64 Docker against clean NanoFaaS `e7914be0`
completed one application `publishRecipe` for all three roles. Independent
registry manifests/configurations, maximum provenance, compiler observations,
frozen receipts and offline receipt verification passed. Real Java probes
confirmed control-plane Serial GC/C1 and Java-function default tier4; Node
version was observed from the published executable image. No second legacy
application build or multi-hour measurement campaign was run.

Full canonical preparation remains **INCOMPLETE**. The normal diagnostic helper
build succeeded, then provider wiring refused the undeclared Node diagnostic
preload. The unchanged prerequisite groups expand to eight coverage IDs; the
freezer separately refuses `async`, `cancellation`, `error`, `late-callback`
and `timeout`, which require fault-capable recipes. Availability declarations
and image receipts cannot supply these missing success proofs.

Private evidence is retained under
`/tmp/nanolab-soak-p24-recipes-verification/`: `native-preparation.log`,
`p24-preparation/gates.json`, publication artifacts and effective-runtime probes.
This is preparation evidence with `p24_qualified: false`. The lower publication
verification does not qualify full preparation, prerequisites or P24 acceptance.

The unchanged recipe smoke regression also completed all phases: 122 steady
requests, zero HTTP errors/drops and 244/244 correctness checks. Its report is
**INCONCLUSIVE** for the three zero-threshold RSS-growth reviews and missing
ownership/equal-work attribution; provenance, preflight, continuity, workload
and artifact-integrity gates pass. Matching teardown returned zero; owned
builders, registry and new image references were removed, while pre-existing
containers/images and the selected builder were preserved. Evidence is under
`smoke-run/` in the same verification directory.
