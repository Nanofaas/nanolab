# Recipe preparation for the P24 container preset

## Purpose and approved scope

Migrate the preparation of `memory-soak-sync-container.yaml` to NanoFaaS
recipes v2. Build and publish its JVM control plane, Java JVM word-stats and
JavaScript word-stats through one reusable profile and one immutable source
snapshot. Reuse the recipe preparation already exercised by the container smoke.

This is the next slice of roadmap step 3. The user approved starting with P24
container preparation, followed separately by other soak backends. The goal is
to simplify build orchestration while preserving the experiment's meaning.
Implementation and real preparation verification establish recipe integration;
they do not establish P24 memory acceptance.

Containerd, Kubernetes, native runtime variants, other P24 diagnostic/spike or
scheduler presets, threshold revisions and new prerequisite coverage are later
work. This slice adds no scheduled action, public publication, release signing,
new generic task type or preparation-only product command. The independently
pending AMD64/ARM64 release qualification does not gate this local Docker work.

## Baseline and approaches

NanoLab base is `4689469`; NanoFaaS remains pinned to `e7914be0`, version
`0.22.0`. Use Python 3.12+ and the existing Sonata dependency pins. The new
`feature/recipe-soak-p24` branch starts from that NanoLab main independently of
the unmerged ARM64 release branch. No NanoFaaS/plugin change is assumed.

The current recipe path rejects `purpose: p24` in both scenario validation and
`validate_soak_recipe`. Preparation itself already supports one snapshot,
one recipe publication, observed build provenance and frozen per-role receipts.
The P24 preset already declares the same three runtime roles and container
provider modules. Its checked-in criteria-only policy exists.

Two approaches were considered:

1. **Extend the existing preparation path to this P24 preset (chosen).** Admit
   P24 through the existing strict validators, add its profile and migrate the
   canonical scenario in place. Keep deployment, prerequisites, measurement and
   evaluation on the existing path.
2. **Introduce a separate P24 recipe runner or prebuilt-image import.** This
   would duplicate ownership/provenance logic or add a receipt-import boundary
   and operator steps. It is unnecessary for the existing three-role build.

The P24 profile is separate from the historical smoke profile so the declared
P24 build settings remain independently reviewable. Both use the same runner
and evidence readers. Preserve the effective legacy launcher settings rather
than copying the smoke profile: Serial GC and C1 for the control plane,
JVM defaults for the Java function. The legacy function Dockerfile ignores
`JVM_TUNING`; empty scenario overrides do not imply equal launcher settings.

## Profile and scenario

Add `packages/nanolab/recipes/soak-container-p24-jvm.yaml` and set
`recipeProfile: ../recipes/soak-container-p24-jvm.yaml` in the existing
`packages/nanolab/scenarios-v2/memory-soak-sync-container.yaml`.

The profile selects exactly:

- JVM control plane, with `container-deployment-provider`, `async-queue` and
  `build-metadata`; Java word-stats in JVM mode; JavaScript word-stats.
- Local registry `127.0.0.1:5000/nanofaas`, one unique per-run tag and provenance
  enabled. Publication tag replacement follows the existing soak contract.
- One platform, `linux/arm64`, matching the checked-in preset. Independent
  native Linux host/daemon checks must pass before building. No emulation or
  silent scenario/platform rewriting.
- Existing effective JVM/build settings: control-plane `jvm.args` exactly
  `-XX:+UseSerialGC`, `-XX:TieredStopAtLevel=1`; no function JVM arguments
  or other launcher, GC, compiler or build overrides. Verify effective runtime and compilation
  settings against the preset and pinned implementation before accepting the
  profile. A discovered mismatch is reported and resolved without silently
  changing the experiment's policy.

Keep `soak.images` as expectations validated against the recipe, as the smoke
integration does. They do not trigger additional legacy builds. Removing these
expectations or introducing a general role-selection abstraction belongs to a
later simplification, after the consumer migrations.

The canonical scenario changes only by adding its recipe reference and updating
comments that describe build preparation. Preserve every other configuration
value, including:

- `purpose: p24`, advanced metrics and the existing criteria-only policy file;
- CPU/memory limits, required metrics/capabilities, runtime options, diagnostics,
  retention, prerequisite coverage and relevant configuration keys;
- warm-up 120 s, baseline drain 2100 s, baseline window 120 s, steady 5400 s,
  final drain 2100 s and cleanup margin 300 s;
- 20 requests/s per function, 200 preallocated/max VUs, zero allowed errors and
  dropped iterations, sampling and evidence/dump budgets.

Do not replace these values with older prose defaults in `docs/soak.md`. Correct
that documentation to the current checked-in scenario and shipped policy while
preserving the distinction between preparation and acceptance.

## Validation and source contract

Extend scenario and recipe validation to support `smoke` and `p24` for this
existing local container JVM/Node combination. Continue rejecting incompatible
backends, runtimes, prebuilt overrides, unsupported build selectors, mismatching
modules/functions/platforms and unsupported recipe options. Use the normal
scenario/policy loader and strict `SoakConfig` validation; changing purpose via
an unchecked model copy must not make a short smoke a valid P24 campaign.

Relative recipe and policy paths resolve against the scenario file. Missing or
invalid profile/policy inputs and conflicting role expectations stop before
Docker provisioning, source capture or publication. `plan` remains non-executing.
CI validates the new profile using pinned `validateRecipe`.

Capture exactly one dirty-inclusive source snapshot for all application roles,
including eligible untracked files under the existing policy. Materialize the
same snapshot into the writable build workspace. Freeze raw recipe bytes,
source manifest, fingerprint, dirty state and original revision. Any synthetic
staging Git revision is identified as staging metadata.

Reuse `prepare_soak_recipe_run`, `publish_soak_recipe`, the owned command executor
and build observer. Invoke `publishRecipe` once, without a preliminary assembly
or per-role build. Retain cancellation, timeout and process-reaping behavior.
A failed or partial publication must not produce completed preparation evidence
or authorize deployment, prerequisites or measurement.

## Evidence, runtime and P24 boundaries

Retain original profile/report bytes, command logs, source binding, observed
compilation/toolchain/base-image provenance and verified registry objects under
the existing evidence budgets. Keep publication/index, executable manifest and
configuration identities distinct. All three frozen image receipts must refer
to their independently verified executable platform images.

The recipe report does not prove the effective compiler or runtime options.
Preserve actual-context observations and the existing provenance/material checks.
Use the same effective JVM and Node options when deployment and diagnostics
consume the prepared artifacts. Additional instrumentation is restricted to
owned materialized inputs and is fingerprinted separately.

The production runtime continues to prepare, deploy and freeze runtime
observations before its existing prerequisite/preflight/measurement stages.
Prerequisites consume the prepared source/config/image identities; recipe
preparation alone supplies no prerequisite success. Keep the existing required
coverage and refusal of ordinary smoke receipts as P24 prerequisite evidence.
Unsupported or missing coverage stays incomplete and prevents qualification.

Passing publication, a runtime probe or the smoke regression never sets
`p24_qualified: true`. Preserve actual numerical findings and incomplete
ownership/equal-work coverage. A qualifying P24 result requires the genuine full
protocol, all existing coverage and evidence gates, and the unchanged criteria.
This migration does not repair or relax existing RSS acceptance limits.

Preserve the existing refusal of `--resume` and partial selections (`--only`,
`--from`, `--until`): soak measurement requires its complete uninterrupted
lifetime. Use a new unused run directory for each experiment and `soak-evaluate`
for saved evidence. No new `--fresh`/resume mechanism is introduced.

Recipe bytes, resolved policy/config, source and image identities remain
binding in frozen evidence and offline verification. Prior legacy P24 evidence
cannot be relabeled as recipe evidence. Missing or altered evidence cannot be
repaired by rebuilding during verification or by converting receipts.

## Resource ownership and verification

Use the existing local registry, owned Buildx builder, selected-builder
preservation and Docker resources. Compensation after cancellation, malformed
reports, failed transport or runtime failure releases only owned resources and
retains diagnostic evidence. Preserve unrelated containers/images and the
operator checkout. Do not clean failed inputs into an apparent successful run.

Required implementation verification:

1. Loader/model tests accept the canonical P24 recipe selection with the shipped
   policy; preserve valid smoke and legacy non-recipe consumers. Reject short
   P24 durations, missing/invalid policy, conflicting profiles and unsupported
   backend/runtime/platform/build overrides before external calls.
2. Compare resolved canonical configuration before/after migration, allowing
   only the recipe reference. Assert unchanged workload, phases, criteria,
   diagnostics, prerequisites and resource/evidence limits.
3. Exercise deferred production preparation with valid P24 configuration:
   exactly one source capture and publication, three complete matching receipts,
   no legacy application builds. Failures/tampering cannot proceed to deployment
   or produce qualifying prerequisite/P24 evidence.
4. Cover offline evidence identity checks, refusal of resumed/partial soak runs
   and cleanup/cancellation boundaries
   through existing workflow/executor tests; no fake successful prerequisite
   receipts and no shortened P24 configuration used as qualification.
5. Run pinned `validateRecipe`, the relevant tests, the full NanoLab suite and
   existing quality/format/security gates. Distinguish existing baseline findings
   from regressions.
6. On the native ARM64 Docker host, run an isolated real preparation verification
   using the same shared production helper and the fully validated P24 config.
   Retain one publication's three registry/config identities, full report/logs,
   source/provenance and effective settings. Any retained probe must consume
   digest-pinned images. This harness is verification evidence, not a new
   product workflow or a P24 campaign. Check ownership and compensate resources.
7. Rerun the existing recipe smoke to verify the shared path's end-to-end
   regression. Its numerical verdict is recorded unchanged; successful
   integration does not require inventing a passing RSS result.

A multi-hour P24 campaign is separate from this preparation migration. Do not
launch one automatically or claim it completed through a shortened run. If real
preparation is blocked by a platform/capability gap, document local progress and
leave the corresponding verification gate open.

## Completion and follow-up

Update the recipe documentation, `docs/soak.md` and roadmap with the reusable
profile, canonical scenario, exact checks and their actual results. Mark only
this preparation slice complete when its required preparation evidence exists;
full P24 qualification, the other P24 presets and other backend migration remain
explicit separate items.

Review the completed implementation against these contracts before integration.
After approval of this written spec, write a task-based implementation plan.
