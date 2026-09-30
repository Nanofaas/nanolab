# Runtime comparison prepared with recipes v2

## Purpose and scope

Migrate the preparation phase of `nanolab compare` from handwritten Gradle,
Docker and push commands to NanoFaaS recipe v2. Keep the comparison's existing
question intact: compare control-plane builds while the Java and JavaScript
function images, cluster, workload, resource limits and measurements are fixed.
The first end-to-end proof is one `jvm` cell with one repetition on Multipass.
The implementation must prepare all nine currently supported variants, including
native builds, but the full matrix is not a gate for this slice.

`compare` remains the owner of VM provisioning, selected variants, cell order,
retry, resume, k6, Prometheus, report generation and cleanup. A recipe describes
only what is built and published. Cells do not run recipe tasks or build images.

## Approaches considered

1. **One checked-in profile per variant, with functions in the `jvm` profile
   (chosen).** Publish `jvm` first and the other selected variants afterward.
   The profiles are directly reusable with `validateRecipe` or `publishRecipe`,
   and the two functions are built exactly once. A selection without `jvm`
   still prepares its JVM distribution to obtain those functions; this costs
   one extra JVM build before the measurement.
2. **Generate a different profile for each run.** This avoids the extra JVM
   build for non-JVM selections, but the effective recipe is hidden in generator
   code and the checked-in profiles cannot be used as-is. It adds branching to
   a preparation phase whose main value is an auditable input.
3. **Add multiple control planes to one recipe.** This would remove repeated
   Gradle calls, but changes the NanoFaaS recipe contract and report schema for
   one NanoLab workflow. The existing recipe contract intentionally has one
   control plane per distribution.

## Profiles and build identity

Add nine profiles under `packages/nanolab/recipes/`, named
`comparison-<variant>.yaml`, for the keys in `VARIANTS_BY_KEY`. Each uses the
VM registry `127.0.0.1:5000/nanofaas`, a distinct control-plane image name
`control-plane-<variant>`, and the same exact modules for all variants:
`k8s-deployment-provider`, `async-queue` and `build-metadata`. NanoFaaS requires
`build-metadata` for an explicit native optimization; the user approved adding
it uniformly rather than changing the plugin contract. Set `build.variant` to
the variant key, so the report and runtime metadata identify the build and its
derived optimization. This adds a module compared with historical matrices;
the resulting samples form a new series and are not equivalent to those runs.
`async-queue` remains the module id in the pinned checkout: it contributes the
`per-function` strategy to the composed `SchedulerEngine` and enables public
asynchronous admission. It does not create a separate scheduler worker.

The `jvm` profile includes both scenario functions: recipe component `word-stats`
with Java JVM build and component `word-stats` with JavaScript SDK, with distinct
image names. Map them to scenario keys `word-stats-java` and
`word-stats-javascript` and verify names and SDKs before provisioning. The other
eight profiles contain no functions.
All profiles declare their JVM arguments or native options explicitly, including
`-XX:+UseSerialGC -XX:TieredStopAtLevel=1` for `jvm`. This corrects NanoLab's
stale assumption that the Dockerfile defaults to C1; NanoFaaS now defaults to
SerialGC with full tiering. The new `jvm` recipe deliberately builds C1 so it
matches the comparison's documented baseline. Historical `jvm` samples made
without an explicit tier flag may have measured C2 and must not be treated as
equivalent to the new samples. Every variant's effective
collector, tier, event-loop setting and native optimization must match its
current label and rationale. Native G1 still requires Oracle GraalVM in the VM.
Preserve `--native-build-memory` and `--native-parallelism` by forwarding their
existing Gradle properties to recipe publication.

Use one unique run tag shared by the nine profiles. Their image names are
distinct, so references cannot collide. Persist a versioned matrix manifest
with the captured NanoFaaS revision and patch SHA-256, recipe hashes, tag,
ordered variant selection, repetitions and native build property overrides.
Also capture and hash the resolved scenario and environment configuration,
including function settings, workload, resource limits and VM configuration,
and record the NanoLab revision and tracked patch hash so changes to k6,
Prometheus queries or orchestration cannot silently change the experiment.
Compare effective configuration values, not just configuration file paths.

On resume, read and validate the existing manifest before writing any manifest,
provisioning or preparing images. All captured inputs must still match. Changed
inputs require a new run directory; `--fresh` does not bypass this check.
Reject nonempty run directories whose manifest is missing, invalid, unsupported
or lacks the required identity fields, including historical comparison runs.
Do not overwrite their manifest or infer missing identities from cell results.

## Preparation and handoff

Capture the NanoFaaS checkout once, including tracked edits, and stage it on
the stack VM. Copy the checked-in profiles into that captured run. Publish the
`jvm` recipe first, then each selected variant other than `jvm`, in one VM
registry and on one VM source checkout. `publishRecipe` performs assembly and
publication in one call. Each invocation has its own output directory; fetch
`distribution.json` and Gradle log to `RUN_DIR/prepare/<variant>/`. Retain the
captured profiles and source identity in the run directory. Cleanup removes
only run-owned VM staging after successful verification; diagnostics remain on
the host after failure. Leave the VM alive through all cells.

Validate the preparation evidence before starting any cell:

- From `distribution.json`, require the recipe SHA-256, run tag, exact module
  set, expected component modes, variant and derived optimization, published
  status, valid manifest digests and exact function set (two for `jvm`, none
  otherwise). Compare reported native
  options to the captured profile. Reject duplicate image references and
  verify each reported digest against its published artifact.
- Require the report's source revision and dirty state to agree with the
  captured checkout. The current report does not contain a patch hash: verify
  the staged tracked source and its patch SHA-256 against the host snapshot
  before each publication, retaining that evidence alongside the report.
  Revision and `dirty` alone do not establish source identity.
- Derive declared JVM arguments and native options from the captured recipe,
  and record forwarded Gradle properties separately. The current report does
  not contain the complete JVM arguments. Inspect the published JVM image's
  argument files and launch configuration to verify the collector, tier and
  event-loop settings, and retain the inspection result. Label declared
  options and artifact verification separately in the manifest.

A failed publication or invalid evidence stops the matrix before deployment
or k6. The shared function images and each control-plane image come from the
validated distributions, never from a constructed variant tag or a fresh build
in a cell.

Pass the selected control-plane reference and the two shared function references
to every cell through the existing prebuilt-image inputs. Use digest-qualified
references when the Kubernetes/Helm path accepts them; otherwise verify the
registry's manifest digest against the report immediately before each cell and
verify the running Pod image identity after deployment. A mutable tag by itself
does not prove a fixed artifact. Check runtime build metadata against the
selected distribution as well. Complete identity checks before starting k6.
Keep the existing load-test and measurement task graph unchanged. The
scenario's `controlPlaneVariant` remains the human
matrix key, while recipe selection belongs to `compare`, not `recipeProfile` on
each cell.

For a new run, write the matrix manifest atomically before preparation so an
interruption remains legible. Record the provisioned VM and cluster identities
before publishing. After each successful publication and validation, update it
atomically with the profile path/hash, distribution path, declared options,
verification evidence, image reference and digest for that variant; record the
shared function references and digests once. Preserve these entries on resume.
The report reader continues to consume existing k6 summaries and metrics
snapshots. A resumed cell must use the same prepared image identities as its
completed peers.

This slice supports resume only on the original stack VM and cluster, with all
previously recorded images still available in its registry. Check those
identities and registry digests before preparing any remaining variants or
running cells. Reuse validated publications; only variants without a committed
publication entry may be prepared from the captured inputs. No cells may start
until every required publication is validated and recorded. If the original
VM, cluster or any recorded image is missing or differs, stop and require a new
run directory. Rebuilding identical source is not a recovery mechanism: mutable
base images and other build inputs can produce different image digests.
Recovery onto a replacement VM would require preserving the original image
manifests and blobs and defining a separate cluster policy; it is outside this
slice. `--fresh` reruns cells under the same checks with the same prepared
identities; a new build needs a new run.

## Verification and documentation

- Validate all nine checked-in profiles with `validateRecipe` against the pinned
  NanoFaaS checkout. Assert exact modules, functions, build flags, image names
  and unique references in focused tests.
- Test preparation with a fake VM publisher: `jvm` first, then each selected
  variant other than `jvm` once, functions exactly once, one source snapshot,
  per-variant reports, and no legacy build operations. Cover invalid reports, staged patch
  mismatches and JVM artifact options that differ from the captured profile.
- Test resume rejection for changed source, recipe, scenario, workload,
  resources, environment, NanoLab identity, variant order or repetitions,
  including with `--fresh`. Reject legacy or incomplete identity manifests
  before overwriting evidence or provisioning. Exercise interruptions before
  and after a publication entry is committed: reuse recorded artifacts and
  prepare only the remaining variants. Missing or changed VM, cluster or
  recorded image identities must stop the run without rebuilding them.
- Test that each cell receives report-derived prebuilt references and that no
  Gradle, Docker build or push task appears in its plan. Preserve the k6 script,
  Prometheus queries, resource settings, interleaving and retry assertions.
- Run a Multipass `compare` with `--variants jvm --repetitions 1`; inspect the
  published report, registry/Pod image identities, k6 summary, metrics snapshot
  and generated comparison report. Numerical equality with an earlier run is
  not expected. Record that this proves the JVM path only; validation and fake
  publisher tests do not establish successful native publication, particularly
  Oracle/G1. Keep that limitation explicit until native execution evidence exists.
- Run Ruff, type checking, relevant NanoLab tests and `git diff --check`.
  Update `packages/nanolab/recipes/README.md`, comparison usage docs and
  `docs/recipes-roadmap.md` after the end-to-end evidence exists.
